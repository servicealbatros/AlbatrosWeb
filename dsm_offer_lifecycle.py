"""Track the same accepted work across an offer and its open DSM work order."""
import json
import re
from dsm_accepted_offers import signature_status, eligibility, acceptance_datetime

SIGNED = {'signed', 'complete', 'completed'}
SENT = {'sent', 'delivered', 'pending_signature'}


def head(payload):
    return payload.get('transaction') or {}


def plate(value):
    return re.sub(r'[^A-Z0-9]', '', str(value or '').upper())


def same_vehicle(left, right):
    a, b = head(left), head(right)
    vin = str(a.get('car_body_series') or '').upper()
    return bool(re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', vin)
        and vin == str(b.get('car_body_series') or '').upper()
        and str(a.get('car_id') or '').isdigit() and int(a['car_id']) > 1
        and str(a['car_id']) == str(b.get('car_id'))
        and plate(a.get('car_auto_number')) and plate(a.get('car_auto_number')) == plate(b.get('car_auto_number')))


def open_order(payload):
    h = head(payload)
    return str(h.get('transaction_head__type')) == '2' and h.get('transaction_head__status') == 'working'


def order_belongs_to_quote(order, quote):
    a, b = head(order), head(quote)
    if not open_order(order) or str(b.get('transaction_head__type')) != '1' or not same_vehicle(order, quote):
        return False
    if b.get('transaction_head__status') in {'canceled', 'cancelled', '4'}:
        return False
    parent = (str(a.get('parent_transaction_type')) == '1'
              and a.get('parent_transaction_code') == b.get('transaction_head__code'))
    child = any(str(c.get('transaction_head__type')) == '2'
                and str(c.get('transaction_id')) == str(a.get('transaction_head__id'))
                and str(c.get('transaction_status')) not in {'4','canceled','cancelled'}
                for c in quote.get('transaction_head_childs') or [])
    return parent or child


def classify(payloads):
    """Descriptors keep conversion separate from proof of an actual appointment."""
    result, owned_orders = [], set()
    for ident, payload in payloads.items():
        h = head(payload)
        if str(h.get('transaction_head__type')) != '1':
            continue
        orders = [(key, p) for key, p in payloads.items() if order_belongs_to_quote(p, payload)]
        state = signature_status(payload)
        source = payload
        extra = {'document_type': '1', 'accepted_at': acceptance_datetime(payload)}
        reason = eligibility(payload)
        if orders:
            owned_orders.update(key for key, _ in orders)
            source = payload if state in SIGNED else next((p for _, p in orders if signature_status(p) in SIGNED), payload)
            state = signature_status(source)
            reason = 'accepted_open_order'
            extra.update(queue_stage='signed_open_order',
                order_ids=[key for key, _ in orders],
                order_codes=[head(p).get('transaction_head__code') or '' for _, p in orders],
                allow_retained_signature=not state)
        elif reason == 'unsigned' and state in SENT and h.get('transaction_head__status') == 'working':
            reason = 'offered'
            extra['queue_stage'] = 'sent_offer'
        result.append(dict(id=ident, code=h.get('transaction_head__code') or '', reason=reason,
                           signature=state, detail_id=ident, extra=extra))
    for ident, payload in payloads.items():
        h = head(payload)
        if str(h.get('transaction_head__type')) != '2' or ident in owned_orders:
            continue
        state = signature_status(payload)
        reason = 'accepted_open_order' if open_order(payload) else 'closed'
        result.append(dict(id=ident, code=h.get('transaction_head__code') or '', reason=reason,
            signature=state, detail_id=ident, extra=dict(document_type='2', queue_stage='signed_open_order',
                order_ids=[ident], order_codes=[h.get('transaction_head__code') or ''],
                origin_offer_code=h.get('parent_transaction_code') if str(h.get('parent_transaction_type')) == '1' else '',
                accepted_at=acceptance_datetime(payload), allow_retained_signature=not state)))
    return result


async def read_snapshot(previous_ids):
    from dsm_open_orders_readonly import _listing_page, _evaluate
    from dsm_history_import import READ_DETAIL_JS
    from dsm_booking_reconciliation import read_offer_details
    ids, catalogue, seen, offset = set(previous_ids), {}, set(), 0
    while True:
        page = await _listing_page(offset)
        rows = page.get('rows')
        if not isinstance(rows, list):
            raise ValueError('Lista documentelor DSM este invalidă.')
        for row in rows:
            ident = str(row.get('transaction_id') or row.get('id') or '')
            if not ident.isdigit() or ident in seen:
                raise ValueError('Lista documentelor DSM este incompletă sau repetată.')
            seen.add(ident)
            kind = str(row.get('transaction_type') or row.get('transaction_head__type'))
            if kind == '1':
                code = row.get('code') or row.get('transaction_head__code') or ''
                catalogue.setdefault(code, set()).add(ident)
            if kind in {'1','2'} and (row.get('status') or row.get('transaction_head__status')) == 'working':
                ids.add(ident)
        offset += len(rows)
        if len(rows) < int(page.get('limit') or 100):
            break
        if offset > 100000:
            raise ValueError('Lista DSM depășește limita de siguranță.')
    if len(ids) > 1500:
        raise ValueError('Prea multe documente active pentru o singură sincronizare.')
    payloads = await read_offer_details(sorted(ids)) if ids else {}
    parents = set()
    for payload in payloads.values():
        h = head(payload)
        if open_order(payload) and str(h.get('parent_transaction_type')) == '1':
            matches = catalogue.get(h.get('parent_transaction_code') or '', set())
            if len(matches) > 1:
                raise ValueError('Legătura dintre ofertă și deviz este ambiguă.')
            parents.update(matches)
    missing = parents - payloads.keys()
    if missing:
        payloads.update(await read_offer_details(sorted(missing)))
    descriptors = classify(payloads)
    wanted = sorted({str(i) for r in descriptors if r['reason'] in {'accepted','accepted_open_order','offered'}
                     for i in [r['detail_id'], *r['extra'].get('order_ids', [])]})
    details = {}
    for start in range(0, len(wanted), 6):
        chunk = wanted[start:start+6]
        values = await _evaluate('('+READ_DETAIL_JS+')('+json.dumps(chunk)+')')
        for value in values:
            detail = value.get('detail') or {}
            ident = str(detail.get('transaction_id') or '')
            if ident not in chunk or ident in details:
                raise ValueError('Citire incompletă a ofertei sau devizului DSM.')
            details[ident] = detail
        if any(ident not in details for ident in chunk):
            raise ValueError('Lipsesc detalii ale ofertelor sau devizelor DSM.')
    result = []
    for row in descriptors:
        detail = details.get(row['detail_id'])
        if detail is not None:
            orders = row['extra'].get('order_ids') or []
            if len(orders) == 1:
                # Use current work-order operations while preserving the original quote identity.
                detail = {**details[str(orders[0])], 'transaction_id': row['id'], 'document_code': row['code']}
            detail = {**detail, **row['extra']}
        result.append(dict(id=row['id'], code=row['code'], reason=row['reason'], signature=row['signature'], detail=detail))
    return result


async def refresh_order_approval(db_path, request, record):
    """Recheck the exact open work order before a human creates a local booking."""
    from datetime import datetime, timezone
    from dsm_accepted_offers import connection
    from dsm_booking_reconciliation import read_offer_details
    detail = json.loads(record['payload_json'])
    ids = detail.get('order_ids') or []
    if len(ids) != 1:
        raise ValueError('Există mai multe devize pentru această ofertă. Verifică devizul exact înainte de programare.')
    order_id, root_id = str(ids[0]), str(record['transaction_id'])
    values = await read_offer_details(sorted({root_id, order_id}))
    root, order = values[root_id], values[order_id]
    h, oh = head(root), head(order)
    if (not open_order(order) or str(h.get('car_body_series') or '').upper() != str(request.get('vin') or '').upper()
            or plate(h.get('car_auto_number')) != plate(request.get('registration'))
            or h.get('transaction_head__code') != record['document_code']
            or not same_vehicle(root, order)
            or (root_id != order_id and not order_belongs_to_quote(order, root))):
        raise ValueError('Oferta sau devizul nu mai corespunde cererii; programarea nu a fost creată.')
    states = {signature_status(root), signature_status(order)}
    signed = bool(states & SIGNED)
    retained = (record['signature_state'] in SIGNED and detail.get('signature_basis') == 'previously_verified_offer_and_exact_open_order'
                and not signature_status(root) and not signature_status(order))
    if not signed and not retained:
        raise ValueError('Semnătura pentru această ofertă sau acest deviz nu mai este confirmată în DSM.')
    approval = dict(kind='working_order', signature='signed', vin=request['vin'],
        offer_id=root_id, offer_code=record['document_code'], order_id=order_id,
        order_code=oh.get('transaction_head__code'), verified_at=datetime.now(timezone.utc).isoformat())
    with connection(db_path) as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS dsm_booking_approvals (
            request_id INTEGER PRIMARY KEY, offer_id TEXT UNIQUE NOT NULL, payload_json TEXT NOT NULL)''')
        for existing in conn.execute('SELECT request_id,payload_json FROM dsm_booking_approvals WHERE request_id!=?', (request['id'],)):
            if json.loads(existing[1]).get('order_id') == order_id:
                raise ValueError('Acest deviz este deja legat de altă cerere din Dashboard.')
        conn.execute('INSERT INTO dsm_booking_approvals VALUES (?,?,?) ON CONFLICT(request_id) DO UPDATE SET offer_id=excluded.offer_id,payload_json=excluded.payload_json',
                     (request['id'], root_id, json.dumps(approval)))
    return approval
