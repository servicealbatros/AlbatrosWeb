"""Link an existing intake to exact, read-only DSM acceptance evidence."""
from datetime import datetime, timedelta, timezone
import json
import math
import re

from dsm_accepted_offers import (
    connection, signature_status, eligibility, acceptance_datetime, offer_validity,
)


def approval_record(db_path, request_id):
    with connection(db_path) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='dsm_booking_approvals'").fetchone():
            return None
        row = conn.execute('SELECT payload_json FROM dsm_booking_approvals WHERE request_id=?', (request_id,)).fetchone()
    return json.loads(row[0]) if row else None


def valid_approval(record, request, now=None):
    if not record or record.get('vin') != str(request.get('vin') or '').strip().upper():
        return False
    now = now or datetime.now(timezone.utc)
    try:
        checked = datetime.fromisoformat(record['verified_at'])
        fresh = timedelta(0) <= now - checked <= timedelta(minutes=15)
    except (KeyError, ValueError, TypeError):
        return False
    if not fresh or record.get('signature') not in {'signed', 'complete', 'completed'}:
        return False
    if record.get('kind') == 'working_order':
        return bool(record.get('order_id'))
    return (record.get('kind') == 'signed_offer'
            and offer_validity({'payload_json': json.dumps(record)}, now)['offer_validity'] == 'valid')


def evaluate_candidates(request, payloads):
    """An unrelated old signed offer, a closed order or a VIN mismatch never qualifies."""
    vin = str(request.get('vin') or '').strip().upper()
    if not re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', vin):
        return []
    heads = {str(p.get('transaction', {}).get('transaction_head__id')): p for p in payloads}
    candidates = []
    for ident, payload in heads.items():
        h = payload.get('transaction') or {}
        signed = signature_status(payload)
        if (str(h.get('transaction_head__type')) != '1' or h.get('car_body_series') != vin
                or signed not in {'signed', 'complete', 'completed'}):
            continue
        base = dict(offer_id=ident, offer_code=h.get('transaction_head__code') or '',
                    vin=vin, signature=signed, accepted_at=acceptance_datetime(payload))
        if eligibility(payload) == 'accepted':
            if offer_validity({'payload_json': json.dumps(base)})['offer_validity'] == 'valid':
                candidates.append(dict(base, kind='signed_offer', technical_hours=_hours(payload)))
            continue
        if h.get('transaction_head__status') not in {'working', 'completed'}:
            continue
        for child in payload.get('transaction_head_childs') or []:
            if str(child.get('transaction_head__type')) != '2' or str(child.get('transaction_status')) in {'4', 'canceled', 'cancelled'}:
                continue
            order_id = str(child.get('transaction_id') or '')
            order = heads.get(order_id) or {}
            oh = order.get('transaction') or {}
            billed = any(str(r.get('transaction_head__type')) in {'4', '5', '6', '10'}
                         and str(r.get('transaction_status')) not in {'4', 'canceled', 'cancelled'}
                         for r in order.get('transaction_head_childs') or [])
            if (str(oh.get('transaction_head__type')) == '2' and oh.get('transaction_head__status') == 'working'
                    and oh.get('car_body_series') == vin and not billed
                    and oh.get('parent_transaction_code') == base['offer_code']):
                candidates.append(dict(base, kind='working_order', order_id=order_id,
                                       order_code=oh.get('transaction_head__code') or '',
                                       technical_hours=_hours(order) or _hours(payload)))
    return candidates


def _hours(payload):
    hours = [float(row.get('transaction_row__qty__line') or 0)
             for row in payload.get('transaction_rows') or [] if row.get('transaction_row__type') == 'W']
    if any(not math.isfinite(value) or value < 0 for value in hours):
        raise ValueError('Timpi DSM invalizi; durata trebuie verificată.')
    return round(sum(hours), 2)


async def refresh_approval(db_path, request):
    from dsm_readonly import _search_transactions, normalize_registration
    from dsm_open_orders_readonly import _evaluate
    if not request.get('registration') or not request.get('vin'):
        return None
    refs = await _search_transactions(normalize_registration(request['registration']))
    ids = [str(row.get('transaction_id') or '') for row in refs
           if str(row.get('transaction_type') or row.get('transaction_head__type')) in {'1', '2'}]
    if any(not ident.isdigit() for ident in ids) or len(ids) > 100:
        raise ValueError('Nu pot stabili fără ambiguitate oferta DSM pentru această cerere.')
    payloads = []
    for offset in range(0, len(ids), 6):
        chunk = ids[offset:offset+6]
        expression = '''(async()=>{const out=[];for(const id of __IDS__){
          const r=await fetch('/special/transaction/get_transaction/'+id,{credentials:'same-origin',signal:AbortSignal.timeout(12000)});
          if(!r.ok||r.url.includes('/login'))throw Error('Autentificare DSM necesară');
          const p=await r.json();if(String(p.transaction?.transaction_head__id)!==id)throw Error('ID DSM neconcordant');
          out.push({transaction:p.transaction,sms_or_qrcode_sended:p.sms_or_qrcode_sended,
                    transaction_head_childs:p.transaction_head_childs,transaction_rows:p.transaction_rows});
          }return JSON.stringify(out)})()'''.replace('__IDS__', json.dumps(chunk))
        payloads.extend(await _evaluate(expression))
    candidates = evaluate_candidates(request, payloads)
    if len(candidates) > 1:
        raise ValueError('Există mai multe oferte/comenzi DSM semnate pentru acest VIN. Selectează oferta exactă înainte de programare.')
    if not candidates:
        with connection(db_path) as conn:
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='dsm_booking_approvals'").fetchone():
                revoked = dict(vin=request.get('vin'), kind='revoked', verified_at=datetime.now(timezone.utc).isoformat())
                conn.execute('UPDATE dsm_booking_approvals SET payload_json=? WHERE request_id=?',
                             (json.dumps(revoked), request['id']))
        for payload in payloads:
            if (eligibility(payload) == 'accepted'
                    and payload.get('transaction', {}).get('car_body_series') == request.get('vin')):
                validity = offer_validity({'payload_json': json.dumps({'accepted_at': acceptance_datetime(payload)})})
                if validity['offer_validity'] != 'valid':
                    raise ValueError(validity['offer_validity_message'])
        return None
    record = dict(candidates[0], verified_at=datetime.now(timezone.utc).isoformat())
    with connection(db_path) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS dsm_booking_approvals (
            request_id INTEGER PRIMARY KEY, offer_id TEXT UNIQUE NOT NULL, payload_json TEXT NOT NULL)''')
        duplicate = conn.execute('SELECT request_id FROM dsm_booking_approvals WHERE offer_id=? AND request_id!=?',
                                 (record['offer_id'], request['id'])).fetchone()
        if duplicate:
            raise ValueError('Oferta DSM este deja legată de cererea #' + str(duplicate[0]) + '.')
        linked = conn.execute("SELECT 1 FROM sqlite_master WHERE name='dsm_accepted_offer_links'").fetchone()
        if linked:
            duplicate = conn.execute('SELECT request_id FROM dsm_accepted_offer_links WHERE transaction_id=? AND request_id!=?',
                                     (record['offer_id'], request['id'])).fetchone()
            if duplicate:
                raise ValueError('Oferta semnată este deja în Dashboard la cererea #' + str(duplicate[0]) + '.')
        conn.execute('INSERT INTO dsm_booking_approvals VALUES (?,?,?) ON CONFLICT(request_id) DO UPDATE SET offer_id=excluded.offer_id,payload_json=excluded.payload_json',
                     (request['id'], record['offer_id'], json.dumps(record)))
        if record['technical_hours'] > 0:
            conn.execute('''UPDATE service_requests SET technical_labour_time=COALESCE(NULLIF(technical_labour_time,0),?),
                booking_time=COALESCE(NULLIF(booking_time,0),?),labour_source=COALESCE(labour_source,?) WHERE id=?''',
                         (record['technical_hours'], record['technical_hours'], 'DSM — ofertă semnată ' + record['offer_code'], request['id']))
    return record
