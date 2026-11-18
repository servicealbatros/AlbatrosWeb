"""Close the dashboard's scheduling task from freshly verified DSM bookings.

All DSM calls are reads. Resolving a request does not finish/cancel its workshop
appointment, create a booking, send a message, or delete the accepted offer.
"""
import asyncio
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dsm_accepted_offers import connection, signature_status

LOCK = asyncio.Lock()


def plate(value):
    return re.sub(r'[^A-Z0-9]', '', str(value or '').upper())


def offer_number(value):
    match = re.fullmatch(r'OFERTA[\s-]+(\d+)', str(value or '').strip().upper())
    return match[1] if match else ''


def candidates(db_path):
    with connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='dsm_accepted_offer_links'").fetchone():
            return [], []
        archived = conn.execute("SELECT 1 FROM sqlite_master WHERE name='request_archive'").fetchone()
        archive_filter = ' AND NOT EXISTS (SELECT 1 FROM request_archive ar WHERE ar.request_id=r.id)' if archived else ''
        rows = conn.execute('''SELECT r.id,r.registration,r.vin,r.status,l.transaction_id,
            l.document_code,l.signature_state,l.payload_json FROM service_requests r
            JOIN dsm_accepted_offer_links l ON l.request_id=r.id
            WHERE r.source='dsm_accepted_offer' AND r.status IN ('acceptat','programat')''' + archive_filter).fetchall()
        result = []
        for row in rows:
            row = dict(row)
            payload = json.loads(row.pop('payload_json'))
            if (row['signature_state'] in {'signed', 'complete', 'completed'}
                    and str(row['transaction_id']).isdigit()
                    and re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', str(row['vin'] or '').upper())
                    and str(payload.get('vin') or '').upper() == str(row['vin']).upper()
                    and plate(row['registration']) and offer_number(row['document_code'])):
                result.append(row)
        if len(result) > 256:
            raise ValueError('Prea multe oferte pentru o singură verificare DSM.')
        appointments = [dict(r) for r in conn.execute('''SELECT * FROM scheduler_appointments
            WHERE status!='anulat' AND dsm_appointment_id IS NOT NULL''')]
        return result, appointments


def appointment_ids(row, appointments, root):
    """Only exact quote references/relationships qualify; a plate alone never does."""
    result = set()
    for child in root.get('transaction_head_childs') or []:
        if (str(child.get('transaction_head__type')) == '3'
                and str(child.get('transaction_status')) not in {'4', 'canceled', 'cancelled'}):
            ident = str(child.get('transaction_id') or '')
            if ident.isdigit():
                result.add(ident)
    head = root.get('transaction') or {}
    if (str(head.get('parent_transaction_type')) == '3'
            and str(head.get('parent_transaction_status')) not in {'4', 'canceled', 'cancelled'}):
        ident = str(head.get('parent_transaction_id') or '')
        if ident.isdigit():
            result.add(ident)
    for appointment in appointments:
        if (appointment.get('source') not in {'dsm_schedule_readonly', 'mechanic_takeover'}
                or appointment.get('request_id') not in (None, row['id'])
                or plate(appointment.get('registration')) != plate(row['registration'])):
            continue
        codes = set(re.findall(r'(?<![A-Z0-9])OFERTA[\s-]+(\d+)(?![A-Z0-9])',
                               (str(appointment.get('job_number') or '') + ' ' + str(appointment.get('operations') or '')).upper()))
        exact_id = (str(appointment.get('dsm_transaction_type')) == '1'
                    and str(appointment.get('dsm_transaction_id')) == row['transaction_id'])
        if not exact_id and codes != {offer_number(row['document_code'])}:
            continue
        match = re.fullmatch(r'(?:APP-)?(\d+)', str(appointment['dsm_appointment_id']))
        if match:
            result.add(match[1])
    claimed = {str(a['dsm_appointment_id']).removeprefix('APP-') for a in appointments
               if a.get('request_id') not in (None, row['id'])}
    return result - claimed


async def read_offer_details(ids):
    from dsm_open_orders_readonly import _evaluate
    details = {}
    for offset in range(0, len(ids), 6):
        chunk = ids[offset:offset+6]
        values = await _evaluate('''(async()=>{
          if(location.protocol!=='https:'||!['s.servicemanager.q-service.ro','apps.dsmpro.ro','appm.dsmpro.ro'].includes(location.hostname))throw Error('Domeniu DSM nepermis');
          return JSON.stringify(await Promise.all(__IDS__.map(async id=>{
          const r=await fetch('/special/transaction/get_transaction/'+id,{credentials:'same-origin',signal:AbortSignal.timeout(12000)});
          if(!r.ok||r.url.includes('/login'))throw Error('Citire DSM indisponibilă');
          const p=await r.json();if(String(p.transaction?.transaction_head__id)!==id)throw Error('ID DSM neconcordant');
          return {transaction:p.transaction,transaction_head_childs:p.transaction_head_childs,sms_or_qrcode_sended:p.sms_or_qrcode_sended};
        })));})()'''.replace('__IDS__', json.dumps(chunk)))
        if (not isinstance(values, list) or len(values) != len(chunk)
                or {str((v.get('transaction') or {}).get('transaction_head__id')) for v in values} != set(chunk)):
            raise ValueError('Citire incompletă a ofertelor DSM.')
        for value in values:
            details[str(value['transaction']['transaction_head__id'])] = value
    return details


async def search_appointments(registration):
    """Read existing document history, including bookings outside the imported week."""
    from dsm_readonly import _search_transactions
    rows = await asyncio.wait_for(_search_transactions(registration), timeout=30)
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError('Istoric DSM incomplet sau prea mare.')
    ids = {str(r.get('transaction_id') or '') for r in rows
           if str(r.get('transaction_type')) == '3'
           and str(r.get('status') or '').lower() not in {'4', 'canceled', 'cancelled'}}
    return {ident for ident in ids if ident.isdigit()}


async def read_appointment(ident):
    from dsm_pre_arrival import appointment_call
    result = await appointment_call('getappointment', {'appointment_id': ident})
    if result.get('status') != 'success' or not isinstance(result.get('detail'), dict):
        raise ValueError('Programarea DSM nu a putut fi verificată.')
    return result['detail']


def verified_booking(row, root, ident, detail):
    head = root.get('transaction') or {}
    if (str(head.get('transaction_head__id')) != row['transaction_id']
            or str(head.get('transaction_head__type')) != '1'
            or str(head.get('transaction_head__status') or '').lower() in {'canceled', 'cancelled', '4'}
            or str(head.get('car_body_series') or '').upper() != str(row['vin']).upper()
            or plate(head.get('car_auto_number')) != plate(row['registration'])
            or offer_number(head.get('transaction_head__code')) != offer_number(row['document_code'])
            or signature_status(root) not in {'signed', 'complete', 'completed'}):
        return None
    if (plate(detail.get('carNumber')) != plate(row['registration'])
            or not str(head.get('car_id') or '').isdigit()
            or int(head['car_id']) <= 1 or str(detail.get('carId')) != str(head['car_id'])
            or str(detail.get('status') or detail.get('appointment_status') or '').lower() in
               {'4', 'canceled', 'cancelled', 'anulat', 'deleted'}
            or detail.get('isCanceled') is True or detail.get('isDeleted') is True
            or str(detail.get('code') or '') != 'APP-' + ident):
        return None
    active = (detail.get('canCancel') is True or detail.get('canTransform') is True
              or str(detail.get('status') or detail.get('appointment_status') or '').lower() in
                 {'working', 'programat', 'confirmed', 'completed'}
              or (str(detail.get('estimateType')) == '2' and str(detail.get('estimateId') or '').isdigit()
                  and int(detail['estimateId']) > 0))
    if not active:
        return None
    try:
        start = datetime.fromisoformat(str(detail.get('start') or '').replace('Z', '+00:00'))
        end = datetime.fromisoformat(str(detail.get('end') or '').replace('Z', '+00:00'))
        if end <= start:
            return None
    except (ValueError, TypeError):
        return None
    live_related = ident in appointment_ids(row, [], root)
    codes = set(re.findall(r'(?<![A-Z0-9])OFERTA[\s-]+(\d+)(?![A-Z0-9])',
                           ' '.join(str(detail.get(k) or '') for k in
                                    ('description', 'services', 'work_summary')).upper()))
    direct = (str(detail.get('estimateId')) == row['transaction_id']
              and str(detail.get('estimateType')) == '1')
    if not live_related and not direct and codes != {offer_number(row['document_code'])}:
        return None
    return dict(request_id=row['id'], registration=row['registration'], offer_id=row['transaction_id'],
                offer_code=row['document_code'], dsm_appointment_id='APP-' + ident,
                start_at=start.isoformat(), end_at=end.isoformat(),
                basis='Exact signed DSM offer and vehicle; existing DSM appointment verified')


async def reconcile(db_path, *, apply=False):
    if LOCK.locked():
        return dict(ok=False, status='running', resolved_request_ids=[])
    async with LOCK:
        rows, appointments = candidates(db_path)
        roots = await read_offer_details(sorted({r['transaction_id'] for r in rows})) if rows else {}
        proofs, pending, cache, searches = [], [], {}, {}
        # No local changes until the complete read succeeds.
        for row in rows:
            root = roots.get(row['transaction_id']) or {}
            matches = []
            registration = plate(row['registration'])
            if registration not in searches:
                searches[registration] = await search_appointments(registration)
            claimed = {str(a['dsm_appointment_id']).removeprefix('APP-') for a in appointments
                       if a.get('request_id') not in (None, row['id'])}
            ids = (appointment_ids(row, appointments, root) | searches[registration]) - claimed
            if len(ids) > 100:
                raise ValueError('Prea multe programări pentru o singură ofertă.')
            for ident in sorted(ids):
                if ident not in cache:
                    cache[ident] = await read_appointment(ident)
                proof = verified_booking(row, root, ident, cache[ident])
                if proof:
                    matches.append(proof)
            if len(matches) == 1:
                proofs.extend(matches)
            else:
                pending.append(dict(request_id=row['id'], registration=row['registration'],
                                    reason='ambiguous_bookings' if matches else 'no_verified_dsm_booking'))
        # Two offers cannot both claim the same calendar entry.
        unique = [p for p in proofs if sum(q['dsm_appointment_id'] == p['dsm_appointment_id'] for q in proofs) == 1]
        pending.extend(dict(request_id=p['request_id'], registration=p['registration'], reason='ambiguous_bookings')
                       for p in proofs if p not in unique)
        resolved = []
        if apply:
            with connection(db_path) as conn:
                conn.execute('BEGIN IMMEDIATE')
                current_rows, current_appointments = candidates(db_path)
                current = {r['id']: r for r in current_rows}
                for proof in unique:
                    row = current.get(proof['request_id'])
                    original = next(r for r in rows if r['id'] == proof['request_id'])
                    if row != original:
                        continue
                    if any(str(a['dsm_appointment_id']).removeprefix('APP-') == proof['dsm_appointment_id'].removeprefix('APP-')
                           and a.get('request_id') not in (None, row['id']) for a in current_appointments):
                        continue
                    conn.execute('''CREATE TABLE IF NOT EXISTS dsm_booking_reconciliations
                        (request_id INTEGER PRIMARY KEY, payload_json TEXT NOT NULL, verified_at TEXT NOT NULL)''')
                    conn.execute('INSERT OR REPLACE INTO dsm_booking_reconciliations VALUES (?,?,?)',
                                 (row['id'], json.dumps(proof), datetime.now(timezone.utc).isoformat()))
                    conn.execute("UPDATE service_requests SET status='rezolvat' WHERE id=? AND status IN ('acceptat','programat')", (row['id'],))
                    resolved.append(row['id'])
        return dict(ok=True, checked=len(rows), verified=unique, pending=pending,
                    resolved_request_ids=resolved, applied=apply)


def state(root):
    try:
        return json.loads((Path(root) / 'dsm_booking_reconciliation.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {'status': 'not_started'}


async def tick(db_path, root):
    info = state(root)
    now = datetime.now(timezone.utc)
    if info.get('checked_at') and (now - datetime.fromisoformat(info['checked_at'])).total_seconds() < 7200:
        return info
    try:
        result = await reconcile(db_path, apply=True)
        if result.get('status') == 'running':
            return info
        info = dict(status='success', checked_at=now.isoformat(), result=result)
    except Exception as exc:
        # Do not persist DSM responses, names, VINs, or credentials in diagnostics.
        info = dict(status='error', checked_at=now.isoformat(), error_type=type(exc).__name__)
    path = Path(root) / 'dsm_booking_reconciliation.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(info, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)
    return info
