"""Read-only DSM acceptance evidence and daily dashboard synchronisation."""
from dsm_open_orders_readonly import _evaluate
import asyncio
from datetime import datetime, timedelta
from contextlib import contextmanager
import html
import json
from pathlib import Path
import re
import sqlite3
import shutil
import os
from uuid import uuid4
from zoneinfo import ZoneInfo

ROMANIA = ZoneInfo('Europe/Bucharest')
LOCK = asyncio.Lock()


def acceptance_datetime(payload):
    """Only explicit signing timestamps count; document/import dates never do."""
    states = payload.get('sms_or_qrcode_sended') or {}
    if not isinstance(states, dict):
        return ''
    dates = set()
    for document, evidence in states.items():
        if re.sub(r'^\d+_', '', str(document)) != 'service_offer' or not isinstance(evidence, dict):
            continue
        status = signature_status({'sms_or_qrcode_sended': {document: evidence}})
        if status not in {'signed', 'complete', 'completed'}:
            continue
        for key in ('signed_at', 'accepted_at', 'signature_datetime', 'acceptance_datetime'):
            raw = evidence.get(key)
            if not isinstance(raw, str):
                continue
            try:
                if re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
                    dates.add(datetime.strptime(raw, '%Y-%m-%d').date().isoformat())
                elif re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?', raw):
                    parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=ROMANIA)
                    dates.add(parsed.astimezone(ROMANIA).isoformat())
            except ValueError:
                continue
    return dates.pop() if len(dates) == 1 else ''


def offer_validity(record, now=None):
    now = (now or datetime.now(ROMANIA)).astimezone(ROMANIA)
    detail = json.loads(record['payload_json']) if record else {}
    accepted = detail.get('accepted_at') or ''
    unknown = {'offer_validity': 'unknown', 'offer_expires_at': '',
               'offer_validity_message': 'Data acceptării trebuie verificată în DSM; valabilitatea de 10 zile nu poate fi confirmată.'}
    if not isinstance(accepted, str) or not accepted:
        return unknown
    try:
        parsed = datetime.fromisoformat(accepted.replace('Z', '+00:00'))
        parsed = parsed.replace(tzinfo=ROMANIA) if parsed.tzinfo is None else parsed.astimezone(ROMANIA)
    except (ValueError, TypeError):
        return unknown
    if parsed > now:
        return unknown
    expiry = parsed + timedelta(days=10)
    date_only = len(accepted) == 10
    expired = now.date() > expiry.date() if date_only else now >= expiry
    return {'offer_validity': 'expired' if expired else 'valid',
            'offer_expires_at': expiry.date().isoformat() if date_only else expiry.isoformat(),
            'offer_validity_message': ('Ofertă expirată — trebuie actualizată înainte de programare.' if expired
                                       else 'Valabilă până la ' + expiry.strftime('%d.%m.%Y' if date_only else '%d.%m.%Y %H:%M'))}


def require_valid_for_booking(db_path, request_row, now=None):
    if not request_row or request_row.get('source') != 'dsm_accepted_offer':
        return
    record = offer_record(db_path, request_row['id'])
    if not has_signed_dashboard_offer(db_path, request_row):
        raise ValueError('Semnătura ofertei DSM nu mai este confirmată pentru programare.')
    validity = offer_validity(record, now)
    if validity['offer_validity'] != 'valid':
        raise ValueError(validity['offer_validity_message'])


def dashboard_dates(record):
    """Keep the technical import time separate from the business event."""
    detail = json.loads(record['payload_json']) if record else {}
    accepted = detail.get('accepted_at') or ''
    return {'accepted_at': accepted, 'request_datetime': accepted,
            'request_date_kind': 'accepted',
            'request_date_missing': not bool(accepted), **offer_validity(record)}


def signature_status(payload):
    """The same document-state mapping used by DSM's green signed icon."""
    states = payload.get('sms_or_qrcode_sended') or {}
    if not isinstance(states, dict):
        return ''
    for document, value in states.items():
        if re.sub(r'^\d+_', '', str(document)) != 'service_offer':
            continue
        if isinstance(value, dict):
            value = next((value[k] for k in ('record_status__code', 'sms_feedback', 'status', 'state') if value.get(k)), '')
        return str(value or '').strip().lower()
    return ''


def eligibility(payload):
    head = payload.get('transaction') or {}
    if str(head.get('transaction_head__type')) != '1':
        return 'not_offer'
    if head.get('transaction_head__status') != 'working':
        return 'closed'
    if signature_status(payload) not in {'complete', 'completed', 'signed'}:
        return 'unsigned'
    related = payload.get('transaction_head_childs') or []
    if not isinstance(related, list):
        raise ValueError('Format necunoscut pentru legăturile ofertei DSM.')
    for child in related:
        if str(child.get('transaction_status')) in {'4', 'canceled', 'cancelled'}:
            continue
        if str(child.get('transaction_head__type')) == '3':
            return 'scheduled'
        if str(child.get('transaction_head__type')) in {'2', '4', '5', '6', '10'}:
            return 'converted'
    if str(head.get('parent_transaction_type')) == '3' and str(head.get('parent_transaction_status')) not in {'canceled', 'cancelled', '4'}:
        return 'scheduled'
    return 'accepted'


@contextmanager
def connection(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_links(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS dsm_accepted_offer_links (
        transaction_id TEXT PRIMARY KEY, request_id INTEGER UNIQUE NOT NULL,
        document_code TEXT NOT NULL, signature_state TEXT NOT NULL,
        eligibility TEXT NOT NULL, payload_json TEXT NOT NULL, updated_at TEXT NOT NULL)''')


def linked_ids(db_path):
    with connection(db_path) as conn:
        init_links(conn)
        return [r[0] for r in conn.execute('SELECT transaction_id FROM dsm_accepted_offer_links')]


async def read_snapshot(previous_ids):
    from dsm_open_orders_readonly import _listing_page
    from dsm_history_import import READ_DETAIL_JS
    refs, seen, offset = {}, set(), 0
    while True:
        page = await _listing_page(offset)
        rows = page.get('rows')
        if not isinstance(rows, list):
            raise ValueError('Lista ofertelor DSM este invalidă.')
        for row in rows:
            ident = str(row.get('transaction_id') or row.get('id') or '')
            if not ident.isdigit() or ident in seen:
                raise ValueError('Paginarea DSM este incompletă sau repetată.')
            seen.add(ident)
            if str(row.get('transaction_type') or row.get('transaction_head__type')) == '1' and row.get('status') == 'working':
                refs[ident] = row
        offset += len(rows)
        if len(rows) < int(page.get('limit') or 100):
            break
        if offset > 100000:
            raise ValueError('Lista DSM depășește limita de siguranță; importul nu este aplicat.')
    ids = list(dict.fromkeys([*refs, *previous_ids]))
    snapshot = []
    for start in range(0, len(ids), 6):
        chunk = ids[start:start+6]
        # Read only the existing transaction endpoint; never accept/sign in DSM.
        expression = r'''(async()=>{return JSON.stringify(await Promise.all(__IDS__.map(async id=>{
          const r=await fetch('/special/transaction/get_transaction/'+id,{credentials:'same-origin',signal:AbortSignal.timeout(12000)});
          if(!r.ok||r.url.includes('/login'))throw Error('Citire DSM incompletă sau autentificare necesară');
          const p=await r.json();if(String(p.transaction?.transaction_head__id)!==String(id))throw Error('ID ofertă DSM neconcordant');
          return {transaction:p.transaction,sms_or_qrcode_sended:p.sms_or_qrcode_sended,transaction_head_childs:p.transaction_head_childs};
        })))} )()'''.replace('__IDS__', json.dumps(chunk))
        values = await _evaluate(expression)
        if not isinstance(values, list) or len(values) != len(chunk):
            raise ValueError('O parte dintre ofertele DSM nu a putut fi citită.')
        for value in values:
            ident = str(value['transaction']['transaction_head__id'])
            reason = eligibility(value)
            detail = None
            if reason == 'accepted':
                details = await _evaluate('('+READ_DETAIL_JS+')('+json.dumps([ident])+')')
                if len(details) != 1 or not details[0].get('detail'):
                    raise ValueError('Datele ofertei semnate sunt incomplete; importul este păstrat.')
                detail = details[0]['detail']
                detail['accepted_at'] = acceptance_datetime(value)
            snapshot.append({'id': ident, 'reason': reason, 'signature': signature_status(value),
                             'code': value['transaction'].get('transaction_head__code') or '', 'detail': detail})
    return snapshot


def apply_snapshot(db_path, snapshot):
    counts = {'imported': 0, 'updated': 0, 'accepted': 0, 'unsigned': 0, 'scheduled': 0, 'closed': 0, 'request_ids': []}
    with connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        init_links(conn)
        conn.execute('BEGIN IMMEDIATE')
        for item in snapshot:
            ident, reason = item['id'], item['reason']
            if not str(ident).isdigit():
                raise ValueError('ID DSM invalid.')
            existing = conn.execute('SELECT * FROM dsm_accepted_offer_links WHERE transaction_id=?', (ident,)).fetchone()
            current = conn.execute('SELECT * FROM service_requests WHERE id=?', (existing['request_id'],)).fetchone() if existing else None
            if reason != 'accepted':
                key = reason if reason in counts else 'closed'
                counts[key] += 1
                if current:
                    conn.execute('UPDATE dsm_accepted_offer_links SET eligibility=?,signature_state=?,updated_at=? WHERE transaction_id=?',
                                 (reason, item['signature'], datetime.now(ROMANIA).isoformat(), ident))
                    active = conn.execute("SELECT 1 FROM scheduler_appointments WHERE request_id=? AND status!='anulat'", (current['id'],)).fetchone()
                    if current['status'] == 'acceptat' and not active:
                        conn.execute('UPDATE service_requests SET status=? WHERE id=?', ('programat' if reason == 'scheduled' else 'ofertat', current['id']))
                continue
            detail = item['detail']
            if not detail or str(detail.get('transaction_id')) != str(ident):
                raise ValueError('Date DSM neconcordante.')
            counts['accepted'] += 1
            operations = [str(r.get('name') or '') for r in detail.get('labors') or [] if r.get('name')]
            symptom = detail.get('description') or '; '.join(operations) or ('Oferta semnată ' + item['code'])
            from scheduler import department_for_operations
            category = 'Tinichigerie–Vopsitorie' if department_for_operations(symptom) == 'tinichigerie_vopsitorie' else 'Mecanică'
            hours = sum(float(r.get('hours') or 0) for r in detail.get('labors') or [])
            fields = {'customer_name': detail.get('customer_name'), 'phone': detail.get('customer_phone'),
                      'registration': detail.get('registration'), 'vin': detail.get('vin'), 'vehicle': detail.get('vehicle'),
                      'mileage': str(detail.get('mileage_km') or ''), 'symptom': symptom,
                      'requested_operations': json.dumps(operations, ensure_ascii=False),
                      'technical_labour_time': hours or None, 'labour_source': 'DSM — ofertă semnată',
                      'labour_operation': '; '.join(operations)}
            if not current:
                columns = {**fields, 'session_id': 'dsm-offer:'+str(ident), 'source': 'dsm_accepted_offer',
                           'conversation_id': 'dsm-offer:'+str(ident), 'status': 'acceptat',
                           'category': category, 'priority': 'normal', 'request_type': 'SERVICE', 'parts_needed': 1, 'labour_needed': 1}
                cursor = conn.execute('INSERT INTO service_requests ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')', list(columns.values()))
                request_id = cursor.lastrowid
                counts['imported'] += 1
            else:
                request_id = current['id']
                # Local cancellations, resolved requests and archive membership survive daily sync.
                if current['status'] in {'acceptat', 'ofertat'}:
                    conn.execute('UPDATE service_requests SET '+','.join(k+'=?' for k in fields)+' WHERE id=?', [*fields.values(), request_id])
                    if current['status'] == 'ofertat' and existing['eligibility'] != 'accepted':
                        conn.execute("UPDATE service_requests SET status='acceptat' WHERE id=?", (request_id,))
                counts['updated'] += 1
            appointment = conn.execute("SELECT id FROM scheduler_appointments WHERE status!='anulat' AND (request_id=? OR (request_id IS NULL AND job_number=?)) LIMIT 1", (request_id, item['code'])).fetchone()
            if appointment:
                conn.execute('UPDATE scheduler_appointments SET request_id=? WHERE id=? AND request_id IS NULL', (request_id, appointment['id']))
                conn.execute("UPDATE service_requests SET status='programat' WHERE id=? AND status NOT IN ('anulat','rezolvat','finalizat')", (request_id,))
            conn.execute('''INSERT INTO dsm_accepted_offer_links VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(transaction_id) DO UPDATE SET request_id=excluded.request_id,document_code=excluded.document_code,
                signature_state=excluded.signature_state,eligibility=excluded.eligibility,payload_json=excluded.payload_json,updated_at=excluded.updated_at''',
                (ident, request_id, item['code'], item['signature'], reason, json.dumps(detail, ensure_ascii=False), datetime.now(ROMANIA).isoformat()))
            counts['request_ids'].append(request_id)
        conn.commit()
    return counts


def state(root):
    try:
        return json.loads((Path(root)/'dsm_accepted_offers_sync.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'enabled': True, 'daily_time': '07:00', 'status': 'not_started'}


def save_state(root, value):
    path = Path(root)/'dsm_accepted_offers_sync.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def due(info, now):
    now = now.astimezone(ROMANIA)
    if not info.get('enabled', True) or info.get('last_success_day', '') >= now.date().isoformat():
        return False
    if now.strftime('%H:%M') < info.get('daily_time', '07:00'):
        return False
    retry = info.get('retry_after')
    return not retry or now >= datetime.fromisoformat(retry)


async def synchronize(db_path, root):
    if LOCK.locked():
        return {'ok': False, 'status': 'running'}
    async with LOCK:
        info = state(root)
        now = datetime.now(ROMANIA)
        info.update(status='running', last_started=now.isoformat())
        save_state(root, info)
        try:
            snapshot = await read_snapshot(linked_ids(db_path))
            result = await asyncio.to_thread(apply_snapshot, db_path, snapshot)
            info.update(status='success', last_success_day=now.date().isoformat(), last_finished=datetime.now(ROMANIA).isoformat(), result=result)
            info.pop('retry_after', None)
            info.pop('error', None)
        except asyncio.CancelledError:
            info.update(status='interrupted', error='Sincronizare întreruptă; datele deja salvate sunt păstrate.')
            raise
        except Exception as exc:
            info.update(status='error', error=str(exc)[:300], retry_after=(datetime.now(ROMANIA)+timedelta(minutes=30)).isoformat())
        finally:
            save_state(root, info)
        return {'ok': info['status'] == 'success', **info}


def offer_record(db_path, request_id):
    with connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='dsm_accepted_offer_links'").fetchone():
            return None
        row = conn.execute('SELECT * FROM dsm_accepted_offer_links WHERE request_id=?', (request_id,)).fetchone()
        return dict(row) if row else None


def has_signed_dashboard_offer(db_path, request_row):
    """Imported DSM approval is separate from approval of a local offer draft."""
    if request_row.get('source') != 'dsm_accepted_offer':
        return False
    record = offer_record(db_path, request_row['id'])
    return bool(record
                and record['signature_state'] in {'signed', 'complete', 'completed'}
                and record['eligibility'] in {'accepted', 'scheduled'})


def rebuild_dashboard(db_path, root, snapshot, *, quoted_ids=(), status_map=None, protected_ids=()):
    """Recoverable removal of old quote cards; never delete requests or bookings."""
    status_map = status_map or {}
    protected_ids = set(protected_ids)
    quoted_ids = set(quoted_ids)
    backup = Path(root) / 'backups' / ('dashboard-offers-' + datetime.now(ROMANIA).strftime('%Y%m%dT%H%M%S') + '-' + uuid4().hex[:8])
    backup.mkdir(parents=True, mode=0o700)
    with connection(db_path) as conn:
        recovery = sqlite3.connect(backup / 'requests.sqlite3')
        try:
            conn.backup(recovery)
        finally:
            recovery.close()
    os.chmod(backup / 'requests.sqlite3', 0o600)
    for name in ('request_status.json', 'offer_drafts.json', 'offer_selections.json'):
        source = Path(root) / name
        if source.is_file():
            shutil.copy2(source, backup / name)
            os.chmod(backup / name, 0o600)
    # Complete DSM reads and durable backup precede any removal from Dashboard.
    imported = apply_snapshot(db_path, snapshot)
    candidates = set(imported['request_ids'])
    archived, restored, kept, untouched = [], [], [], []
    with connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE IF NOT EXISTS request_archive(request_id INTEGER PRIMARY KEY, archived_at TEXT DEFAULT CURRENT_TIMESTAMP)')
        conn.execute('BEGIN IMMEDIATE')
        archived_before = {r[0] for r in conn.execute('SELECT request_id FROM request_archive')}
        rows = conn.execute('SELECT id,status,source FROM service_requests').fetchall()
        for row in rows:
            ident = row['id']
            active = conn.execute("SELECT 1 FROM scheduler_appointments WHERE request_id=? AND status!='anulat' LIMIT 1", (ident,)).fetchone()
            if ident in candidates and row['status'] == 'acceptat' and not active:
                kept.append(ident)
                if ident in archived_before:
                    conn.execute('DELETE FROM request_archive WHERE request_id=?', (ident,))
                    restored.append(ident)
                continue
            if ident in archived_before:
                continue
            state = str(status_map.get(ident, status_map.get(str(ident), row['status'])) or '').lower()
            is_old_quote = (row['source'] == 'dsm_accepted_offer' or ident in quoted_ids
                            or state in {'ofertat', 'acceptat', 'programat', 'anulat', 'rezolvat', 'finalizat'}
                            or row['status'] in {'ofertat', 'acceptat', 'programat', 'anulat', 'rezolvat', 'finalizat'})
            if is_old_quote and ident not in protected_ids:
                conn.execute('INSERT OR IGNORE INTO request_archive(request_id) VALUES (?)', (ident,))
                archived.append(ident)
            else:
                untouched.append(ident)
    return {'ok': True, 'accepted': len(kept), 'request_ids': kept, 'archived': len(archived),
            'archived_ids': archived, 'restored': len(restored), 'untouched_ids': untouched,
            'protected_ids': sorted(protected_ids), 'backup': str(backup), 'sync': imported}


async def rebuild_signed_offers(db_path, root, **options):
    if LOCK.locked():
        return {'ok': False, 'status': 'running'}
    async with LOCK:
        snapshot = await read_snapshot(linked_ids(db_path))
        result = await asyncio.to_thread(rebuild_dashboard, db_path, root, snapshot, **options)
        info = state(root)
        info.update(status='success', last_success_day=datetime.now(ROMANIA).date().isoformat(),
                    last_finished=datetime.now(ROMANIA).isoformat(), result=result['sync'])
        info.pop('error', None)
        info.pop('retry_after', None)
        save_state(root, info)
        return result


def offer_html(record, request_row):
    detail = json.loads(record['payload_json'])
    validity = offer_validity(record)
    escape = lambda x: html.escape(str('—' if x is None or x == '' else x))
    signed_label = 'Ofertă semnată în DSM' if record['signature_state'] in {'signed', 'complete', 'completed'} else 'Semnătura ofertei nu mai este confirmată în DSM'
    rows = []
    for kind, label in (('parts', 'Piesă'), ('labors', 'Manoperă'), ('other_rows', 'Altele')):
        for row in detail.get(kind) or []:
            rows.append('<tr>'+''.join('<td>'+escape(x)+'</td>' for x in (label, row.get('code'), row.get('name'), row.get('quantity'), row.get('historical_price_ron')) )+'</tr>')
    return f'''<!doctype html><html lang="ro"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>{escape(record['document_code'])} — ALBATROS A</title><style>body{{font:16px system-ui;background:#f3f6fa;color:#17253a;margin:0;padding:24px}}main{{max-width:1100px;margin:auto;background:white;padding:24px;border-radius:16px}}a{{color:#1858a0}}.badge{{background:#fff0cc;padding:10px;border-radius:10px}}table{{border-collapse:collapse;width:100%;margin-top:24px}}td,th{{text-align:left;padding:12px;border-bottom:1px solid #dde3ed}}.scroll{{overflow:auto}}p{{white-space:pre-wrap}}</style>
    <main><nav><a href="/cereri">← Dashboard</a> · <a href="/programator">Deschide programatorul</a></nav>
    <h1>{escape(record['document_code'])} · {escape(detail.get('registration'))}</h1>
    <div class="badge">{escape(signed_label)} · {escape('Acceptată — în așteptarea programării' if request_row.get('status') == 'acceptat' else request_row.get('status'))}</div>
    <p>Data acceptării: {escape(detail.get('accepted_at') or 'indisponibilă în DSM')}</p>
    <p><strong>{escape(validity['offer_validity_message'])}</strong></p>
    <h2>{escape(detail.get('vehicle'))}</h2><p>{escape(detail.get('customer_name'))} · {escape(detail.get('customer_phone'))}\nVIN: {escape(detail.get('vin'))}</p>
    <p>{escape(detail.get('description'))}</p><div class="scroll"><table><thead><tr><th>Tip</th><th>Cod</th><th>Lucrare / piesă</th><th>Cantitate / ore</th><th>Preț unitar DSM (RON, cu TVA)</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
    <p>Oferta acceptată este preluată din DSM. Prețurile sunt cele citite din ofertă; nu sunt recalculate din catalog. Ultima sincronizare: {escape(record['updated_at'])}</p></main></html>'''
