"""Read-only DSM acceptance evidence and hourly dashboard synchronisation."""
from dsm_open_orders_readonly import _evaluate
import asyncio
from datetime import datetime, timedelta, timezone
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
SYNC_INTERVAL = timedelta(hours=1)


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
    from dsm_booking_approval import approval_record, valid_approval
    evidence = approval_record(db_path, request_row['id']) if request_row else None
    if evidence:
        if valid_approval(evidence, request_row, now):
            return
        raise ValueError('Confirmarea comenzii DSM trebuie reverificată înainte de programare. Reia acțiunea din Dashboard.')
    if not request_row or request_row.get('source') != 'dsm_accepted_offer':
        return
    record = offer_record(db_path, request_row['id'])
    if not has_signed_dashboard_offer(db_path, request_row):
        raise ValueError('Semnătura ofertei DSM nu mai este confirmată pentru programare.')
    if json.loads(record['payload_json']).get('order_ids'):
        raise ValueError('Devizul deschis trebuie reverificat în DSM înainte de programare.')
    validity = offer_validity(record, now)
    if validity['offer_validity'] != 'valid':
        raise ValueError(validity['offer_validity_message'])


def dashboard_dates(record):
    """Keep the technical import time separate from the business event."""
    detail = json.loads(record['payload_json']) if record else {}
    accepted = detail.get('accepted_at') or ''
    signature = record.get('signature_state', '') if record else ''
    open_orders = detail.get('order_ids') or []
    projected = {'accepted_at': accepted, 'request_datetime': accepted,
            'request_date_kind': 'accepted',
            'request_date_missing': not bool(accepted), **offer_validity(record),
            'dsm_signature_confirmed': signature in {'signed','complete','completed'},
            'dsm_queue_stage': detail.get('queue_stage') or '',
            'dsm_order_ids': open_orders, 'dsm_order_codes': detail.get('order_codes') or [],
            'dsm_has_open_order': bool(open_orders and detail.get('queue_stage') == 'signed_open_order')}
    if projected['dsm_has_open_order']:
        projected['offer_validity_message'] = 'Deviz deschis în DSM; acceptul și lucrarea vor fi reverificate înainte de programare.'
    projected['dsm_lifecycle_label'] = (
        'Semnată · deviz deschis: ' + ', '.join(projected['dsm_order_codes']) if projected['dsm_has_open_order'] and projected['dsm_signature_confirmed']
        else 'Semnată în DSM' if projected['dsm_signature_confirmed']
        else 'Deviz deschis · semnătură de verificat' if open_orders
        else 'Ofertă trimisă · așteaptă semnătura' if detail.get('queue_stage') == 'sent_offer'
        else 'Semnătură neconfirmată în DSM')
    return projected


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
    from dsm_offer_lifecycle import read_snapshot as read_lifecycle
    return await read_lifecycle(previous_ids)


def apply_snapshot(db_path, snapshot, *, include_unconfirmed=False):
    counts = {'imported': 0, 'updated': 0, 'accepted': 0, 'offered': 0, 'converted_waiting': 0, 'unsigned': 0, 'scheduled': 0, 'closed': 0, 'request_ids': []}
    with connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        init_links(conn)
        conn.execute('BEGIN IMMEDIATE')
        for item in snapshot:
            ident, reason = item['id'], item['reason']
            if not str(ident).isdigit():
                raise ValueError('ID DSM invalid.')
            existing = conn.execute('SELECT * FROM dsm_accepted_offer_links WHERE transaction_id=?', (ident,)).fetchone()
            # If DSM exposes the parent quote later, retain the already imported work-order card.
            detail = item.get('detail') or {}
            order_ids = detail.get('order_ids') or []
            if not existing and detail.get('document_type') == '1' and len(order_ids) == 1:
                alias = conn.execute('SELECT * FROM dsm_accepted_offer_links WHERE transaction_id=?', (str(order_ids[0]),)).fetchone()
                if (alias and json.loads(alias['payload_json']).get('document_type') == '2'
                        and json.loads(alias['payload_json']).get('vin') == detail.get('vin')):
                    conn.execute('UPDATE dsm_accepted_offer_links SET transaction_id=? WHERE transaction_id=?', (ident, alias['transaction_id']))
                    existing = conn.execute('SELECT * FROM dsm_accepted_offer_links WHERE transaction_id=?', (ident,)).fetchone()
            current = conn.execute('SELECT * FROM service_requests WHERE id=?', (existing['request_id'],)).fetchone() if existing else None
            item = dict(item)
            if reason == 'accepted_open_order':
                detail = dict(item['detail'] or {})
                signed = item['signature'] in {'signed','complete','completed'}
                if (not signed and detail.get('allow_retained_signature') and existing and current
                        and existing['signature_state'] in {'signed','complete','completed'}
                        and str(current['vin'] or '').upper() == str(detail.get('vin') or '').upper()):
                    item['signature'] = existing['signature_state']
                    detail['signature_basis'] = 'previously_verified_offer_and_exact_open_order'
                    signed = True
                reason = 'accepted' if signed else 'offered' if include_unconfirmed or item['signature'] in {'sent','delivered','pending_signature'} else 'unsigned'
                if signed:
                    counts['converted_waiting'] += 1
                else:
                    detail['queue_stage'] = 'open_order_review'
                item['detail'] = detail
            if reason not in {'accepted','offered'}:
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
            counts['accepted' if reason == 'accepted' else 'offered'] += 1
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
                           'conversation_id': 'dsm-offer:'+str(ident), 'status': 'acceptat' if reason == 'accepted' else 'ofertat',
                           'category': category, 'priority': 'normal', 'request_type': 'SERVICE', 'parts_needed': 1, 'labour_needed': 1}
                cursor = conn.execute('INSERT INTO service_requests ('+','.join(columns)+') VALUES ('+','.join('?' for _ in columns)+')', list(columns.values()))
                request_id = cursor.lastrowid
                counts['imported'] += 1
            else:
                request_id = current['id']
                # Local cancellations, resolved requests and archive membership survive daily sync.
                if current['status'] in {'acceptat', 'ofertat'}:
                    conn.execute('UPDATE service_requests SET '+','.join(k+'=?' for k in fields)+' WHERE id=?', [*fields.values(), request_id])
                    if reason == 'offered' or (current['status'] == 'ofertat' and existing['eligibility'] != 'accepted'):
                        conn.execute('UPDATE service_requests SET status=? WHERE id=?',
                                     ('acceptat' if reason == 'accepted' else 'ofertat', request_id))
                counts['updated'] += 1
            appointment = conn.execute("SELECT id FROM scheduler_appointments WHERE status!='anulat' AND (request_id=? OR (request_id IS NULL AND job_number=?)) LIMIT 1", (request_id, item['code'])).fetchone()
            if reason == 'accepted' and appointment and not detail.get('order_ids'):
                conn.execute('UPDATE scheduler_appointments SET request_id=? WHERE id=? AND request_id IS NULL', (request_id, appointment['id']))
                conn.execute("UPDATE service_requests SET status='programat' WHERE id=? AND status NOT IN ('anulat','rezolvat','finalizat')", (request_id,))
            conn.execute('''INSERT INTO dsm_accepted_offer_links VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(transaction_id) DO UPDATE SET request_id=excluded.request_id,document_code=excluded.document_code,
                signature_state=excluded.signature_state,eligibility=excluded.eligibility,payload_json=excluded.payload_json,updated_at=excluded.updated_at''',
                (ident, request_id, item['code'], item['signature'], reason, json.dumps(detail, ensure_ascii=False), datetime.now(ROMANIA).isoformat()))
            counts['request_ids'].append(request_id)
        conn.commit()
    counts['programmed_request_ids'] = reconcile_existing_bookings(db_path)
    return counts


def state(root):
    try:
        info = json.loads((Path(root)/'dsm_accepted_offers_sync.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        info = {'enabled': True, 'status': 'not_started'}
    return _hourly_state(info)


def _utc_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ROMANIA)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _hourly_state(info):
    info = dict(info)
    info.pop('daily_time', None)
    info['interval_hours'] = 1
    # Migrate the last successful daily run without reusing a failed finish time.
    if info.get('status') == 'success' and _utc_time(info.get('last_finished')):
        info['last_success_at'] = info['last_finished']
    last = _utc_time(info.get('last_success_at'))
    retry = _utc_time(info.get('retry_after'))
    next_due = last + SYNC_INTERVAL if last else None
    if retry and (next_due is None or retry > next_due):
        next_due = retry
    info['next_due'] = next_due.astimezone(ROMANIA).isoformat() if next_due and info.get('enabled', True) else None
    return info


def save_state(root, value):
    hourly = _hourly_state(value)
    value.clear()
    value.update(hourly)
    path = Path(root)/'dsm_accepted_offers_sync.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def due(info, now):
    info = _hourly_state(info)
    if not info.get('enabled', True):
        return False
    now = _utc_time(now.isoformat())
    retry = _utc_time(info.get('retry_after'))
    if retry and now < retry:
        return False
    last = _utc_time(info.get('last_success_at'))
    return last is None or now >= last + SYNC_INTERVAL


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
            result = await asyncio.to_thread(apply_snapshot, db_path, snapshot,
                include_unconfirmed=bool(info.get('include_open_orders_for_review', False)))
            from dsm_billing_sync import reconcile_billed_requests
            result['billing'] = await reconcile_billed_requests(db_path)
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


def reconcile_existing_bookings(db_path, request_id=None, now=None):
    """Attach an existing DSM booking, never approve or create a new booking.

    DSM's separate calendar can reference an offer only in its description.
    Require the exact offer number AND plate, signed VIN evidence, and a unique
    active calendar entry. An unknown signature date remains unknown.
    """
    now = (now or datetime.now(ROMANIA)).astimezone(ROMANIA)
    with connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        columns = {r[1] for r in conn.execute('PRAGMA table_info(scheduler_appointments)')}
        if not {'id', 'request_id', 'job_number', 'status', 'source', 'dsm_appointment_id',
                'registration', 'operations', 'end_at'}.issubset(columns):
            return []
        conn.execute('BEGIN IMMEDIATE')
        rows = conn.execute('''SELECT r.*, l.document_code, l.payload_json, l.signature_state, l.eligibility
            FROM service_requests r JOIN dsm_accepted_offer_links l ON l.request_id=r.id
            WHERE r.source='dsm_accepted_offer' AND r.status IN ('acceptat','ofertat','programat')
            AND (? IS NULL OR r.id=?)''', (request_id, request_id)).fetchall() if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name='dsm_accepted_offer_links'").fetchone() else []
        appointments = conn.execute("""SELECT * FROM scheduler_appointments
            WHERE source IN ('dsm_schedule_readonly','mechanic_takeover')
            AND dsm_appointment_id IS NOT NULL AND dsm_appointment_id!=''
            AND status NOT IN ('anulat','finalizat')""").fetchall()
        active = []
        for appointment in appointments:
            try:
                end = datetime.fromisoformat(appointment['end_at'])
                end = end.replace(tzinfo=ROMANIA) if end.tzinfo is None else end.astimezone(ROMANIA)
            except (ValueError, TypeError):
                continue
            if end >= now:
                active.append(appointment)
        linked = set()
        # A request already attached to an imported APP keeps that identity even
        # when the calendar description no longer repeats the quote number.
        for appointment in active:
            ident = appointment['request_id']
            if not ident or (request_id is not None and ident != request_id):
                continue
            request = conn.execute('SELECT registration,status FROM service_requests WHERE id=?', (ident,)).fetchone()
            if not request or request['status'] in {'anulat','rezolvat','finalizat'}:
                continue
            plate = re.sub(r'[^A-Z0-9]', '', str(request['registration'] or '').upper())
            if plate and plate == re.sub(r'[^A-Z0-9]', '', str(appointment['registration'] or '').upper()):
                linked.add(ident)
        proposals = {}
        for row in rows:
            if row['signature_state'] not in {'signed','complete','completed'} or row['eligibility'] not in {'accepted','scheduled'}:
                continue
            detail = json.loads(row['payload_json'])
            if detail.get('order_ids'):
                # Converted orders use fresh native relationships, never a plate-only calendar match.
                continue
            vin = str(row['vin'] or '').strip().upper()
            if not re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', vin) or str(detail.get('vin') or '').strip().upper() != vin:
                continue
            code = re.fullmatch(r'OFERTA[\s-]+(\d+)', str(row['document_code'] or '').strip().upper())
            plate = re.sub(r'[^A-Z0-9]', '', str(row['registration'] or '').upper())
            if not code or not plate:
                continue
            matches = []
            for appointment in active:
                if appointment['request_id'] not in (None, row['id']):
                    continue
                if re.sub(r'[^A-Z0-9]', '', str(appointment['registration'] or '').upper()) != plate:
                    continue
                # A bare plate, a similar offer number, or another offer for the
                # same car is insufficient evidence.
                text = str(appointment['job_number'] or '') + ' ' + str(appointment['operations'] or '')
                codes = set(re.findall(r'(?<![A-Z0-9])OFERTA[\s-]+(\d+)(?![A-Z0-9])', text.upper()))
                if codes == {code[1]}:
                    matches.append(appointment)
            if len(matches) == 1:
                proposals[row['id']] = matches[0]['id']
        for ident, appointment_id in proposals.items():
            if list(proposals.values()).count(appointment_id) != 1:
                continue
            conn.execute('UPDATE scheduler_appointments SET request_id=? WHERE id=? AND request_id IS NULL', (ident, appointment_id))
            linked.add(ident)
        for ident in linked:
            conn.execute("UPDATE service_requests SET status='programat' WHERE id=? AND status!='programat'", (ident,))
        return sorted(linked)


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
    validity = dashboard_dates(record)
    escape = lambda x: html.escape(str('—' if x is None or x == '' else x))
    signed_label = validity['dsm_lifecycle_label']
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
