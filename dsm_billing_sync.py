"""Resolve local requests only from exact, read-only DSM work-order billing."""
import json
import re
import sqlite3
from datetime import datetime, timezone

from dsm_accepted_offers import connection


def bindings(db_path):
    with connection(db_path) as conn:
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'service_requests' not in tables:
            return []
        requests = {r['id']: dict(r) for r in conn.execute(
            "SELECT id,vin FROM service_requests WHERE status NOT IN ('rezolvat','anulat','finalizat')")}
        result = {}
        for table, ident_column in (('dsm_accepted_offer_links','transaction_id'), ('dsm_booking_approvals','offer_id')):
            if table not in tables:
                continue
            for row in conn.execute('SELECT request_id,'+ident_column+' AS offer_id,payload_json FROM '+table):
                request = requests.get(row['request_id'])
                if not request:
                    continue
                vin = str(request['vin'] or '').strip().upper()
                offer_id = str(row['offer_id'])
                if not re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', vin) or not offer_id.isdigit():
                    continue
                payload = json.loads(row['payload_json'])
                if str(payload.get('vin') or '').strip().upper() != vin:
                    continue
                key = (request['id'], offer_id)
                binding = result.setdefault(key, dict(request_id=request['id'], offer_id=offer_id, vin=vin,
                                                       offer_code='', order_id=''))
                binding['offer_code'] = payload.get('document_code') or payload.get('offer_code') or binding['offer_code']
                binding['order_id'] = str(payload.get('order_id') or binding['order_id'])
        # Conflicting links are never settled by choosing the first offer.
        return [r for r in result.values() if sum(k[0] == r['request_id'] for k in result) == 1]


async def _read_details(ids):
    from dsm_open_orders_readonly import _evaluate
    ids = sorted(set(ids))
    if len(ids) > 512 or any(not str(i).isdigit() for i in ids):
        raise ValueError('Legături DSM invalide sau prea numeroase pentru verificarea facturării.')
    details = {}
    for offset in range(0, len(ids), 6):
        expression = '''(async()=>JSON.stringify(await Promise.all(__IDS__.map(async id=>{
          const r=await fetch('/special/transaction/get_transaction/'+id,{credentials:'same-origin',signal:AbortSignal.timeout(12000)});
          if(!r.ok||r.url.includes('/login'))throw Error('Nu pot verifica facturarea în DSM; autentificare sau citire necesară');
          const p=await r.json();if(String(p.transaction?.transaction_head__id)!==id)throw Error('ID DSM neconcordant la facturare');
          return {transaction:p.transaction,transaction_head_childs:p.transaction_head_childs};
        }))))()'''.replace('__IDS__', json.dumps(ids[offset:offset+6]))
        values = await _evaluate(expression)
        if not isinstance(values, list) or len(values) != len(ids[offset:offset+6]):
            raise ValueError('Citire incompletă a facturării DSM; statusurile sunt păstrate.')
        for payload in values:
            ident = str((payload.get('transaction') or {}).get('transaction_head__id') or '')
            if ident not in ids[offset:offset+6] or ident in details:
                raise ValueError('Documente DSM neconcordante la verificarea facturării.')
            details[ident] = payload
    return details


def billed_evidence(binding, details):
    root = details.get(binding['offer_id']) or {}
    head = root.get('transaction') or {}
    code = str(head.get('transaction_head__code') or '')
    if (str(head.get('transaction_head__id')) != binding['offer_id']
            or str(head.get('transaction_head__type')) != '1'
            or head.get('car_body_series') != binding['vin']
            or head.get('transaction_head__status') in {'canceled','cancelled'}
            or (binding['offer_code'] and code != binding['offer_code'])):
        return None
    order_ids = {str(c.get('transaction_id') or '') for c in root.get('transaction_head_childs') or []
                 if str(c.get('transaction_head__type')) == '2'
                 and str(c.get('transaction_status')) not in {'4','canceled','cancelled'}}
    if binding['order_id']:
        order_ids.add(binding['order_id'])
    if not order_ids:
        return None
    order_codes = []
    for ident in sorted(order_ids):
        order = (details.get(ident) or {}).get('transaction') or {}
        # An advance invoice or a merely closed order must not resolve the job.
        if (str(order.get('transaction_head__id')) != ident
                or str(order.get('transaction_head__type')) != '2'
                or order.get('transaction_head__status') != 'billed'
                or order.get('car_body_series') != binding['vin']
                or order.get('parent_transaction_code') != code
                or str(order.get('parent_transaction_type')) != '1'):
            return None
        order_codes.append(str(order.get('transaction_head__code') or ''))
    return dict(offer_id=binding['offer_id'], offer_code=code, vin=binding['vin'],
                order_ids=sorted(order_ids), order_codes=order_codes, basis='DSM work order status billed')


async def reconcile_billed_requests(db_path):
    linked = bindings(db_path)
    if not linked:
        return dict(checked=0, resolved_request_ids=[])
    details = await _read_details([r['offer_id'] for r in linked])
    order_ids = {str(c.get('transaction_id') or '') for p in details.values()
                 for c in p.get('transaction_head_childs') or []
                 if str(c.get('transaction_head__type')) == '2'
                 and str(c.get('transaction_status')) not in {'4','canceled','cancelled'}}
    order_ids.update(r['order_id'] for r in linked if r['order_id'])
    details.update(await _read_details(order_ids))
    evidence = {r['request_id']: billed_evidence(r, details) for r in linked}
    resolved = []
    with connection(db_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = {r['request_id']: r for r in bindings(db_path)}
        for ident, proof in evidence.items():
            if not proof or ident not in current or current[ident]['offer_id'] != proof['offer_id'] or current[ident]['vin'] != proof['vin']:
                continue
            changed = conn.execute("UPDATE service_requests SET status='rezolvat' WHERE id=? AND vin=? AND status NOT IN ('rezolvat','anulat','finalizat')",
                                   (ident, proof['vin'])).rowcount
            if not changed:
                continue
            conn.execute('''CREATE TABLE IF NOT EXISTS dsm_billing_reconciliations
                (request_id INTEGER PRIMARY KEY, payload_json TEXT NOT NULL, verified_at TEXT NOT NULL)''')
            conn.execute('INSERT OR REPLACE INTO dsm_billing_reconciliations VALUES (?,?,?)',
                         (ident, json.dumps(proof), datetime.now(timezone.utc).isoformat()))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='scheduler_appointments'").fetchone():
                conn.execute("UPDATE scheduler_appointments SET status='finalizat',updated_at=CURRENT_TIMESTAMP WHERE request_id=? AND status NOT IN ('anulat','finalizat')", (ident,))
            resolved.append(ident)
    return dict(checked=len(linked), resolved_request_ids=resolved)
