"""Narrow DSM writer for the explicitly authorised B101TST pilot only."""
import json
import re
from dsm_open_orders_readonly import _evaluate
from dsm_accepted_offers import eligibility

OFFER = '19527'
VIN = 'WBA00000000000002'
PLATE = 'B101TST'


async def call(path, data=None):
    # No arbitrary hosts/routes from messages or OCR.
    if not re.fullmatch(r'/special/(transaction/(get_transaction/\d+|offer_accepted/19527|update_transaction_details)|car/get_car|contact/get_client)', path):
        raise ValueError('Operație DSM nepermisă în pilot.')
    return await _evaluate('''(async()=>{
      if(location.protocol!=='https:'||!['s.servicemanager.q-service.ro','apps.dsmpro.ro','appm.dsmpro.ro'].includes(location.hostname))throw Error('Domeniu DSM nepermis');
      const data=__DATA__; const r=await fetch(__PATH__,{method:data?'POST':'GET',credentials:'same-origin',headers:{Accept:'application/json','Content-Type':'application/x-www-form-urlencoded','X-CSRF-TOKEN':document.querySelector('meta[name="csrf-token"]')?.content||''},...(data?{body:new URLSearchParams(data)}:{}),signal:AbortSignal.timeout(12000)});
      if(!r.ok||r.url.includes('/login'))throw Error('DSM indisponibil sau sesiune expirată');
      return JSON.stringify(await r.json());})()'''.replace('__DATA__', json.dumps(data)).replace('__PATH__', json.dumps(path)))


async def source():
    p = await call('/special/transaction/get_transaction/' + OFFER)
    h = p.get('transaction') or {}
    if str(h.get('transaction_head__id')) != OFFER or str(h.get('car_body_series', '')).upper() != VIN or re.sub(r'\W', '', h.get('car_auto_number', '')).upper() != PLATE:
        raise ValueError('Oferta DSM nu corespunde mașinii testate.')
    return p


def children(p):
    return [str(c['transaction_id']) for c in p.get('transaction_head_childs', [])
            if str(c.get('transaction_head__type')) == '2' and str(c.get('transaction_status')) not in {'4', 'canceled', 'cancelled'}]


async def seed():
    p = await source()
    if eligibility(p) != 'accepted':
        raise ValueError('Oferta nu mai este semnată și în așteptare; verificare necesară.')
    h = p['transaction']
    return {'offer_id': OFFER, 'registration': PLATE, 'vin': VIN,
            'work': str(h.get('transaction_head__description') or '') or '; '.join(str(r.get('transaction_row__name', '')) for r in p.get('transaction_rows', [])),
            'source_code': str(h.get('transaction_head__code') or OFFER)}


async def create_or_find(data, allow_create):
    if data.get('vin') != VIN or data.get('registration') != PLATE or not data.get('final_confirmed') or not data.get('document_verified'):
        raise ValueError('Datele finale ale pilotului nu sunt confirmate.')
    p = await source()
    ids = children(p)
    if len(ids) > 1:
        raise ValueError('Mai multe comenzi legate de ofertă; verificare manuală.')
    if ids:
        command_id = ids[0]
    else:
        if not allow_create or eligibility(p) != 'accepted':
            raise ValueError('Rezultatul creării este incert. Verifică DSM înainte de reluare.')
        result = await call('/special/transaction/offer_accepted/' + OFFER,
                            {'payment_advance': 'false', 'advance_value': '', 'transform_type': 'estimate', 'transform_type_appointment': '0'})
        command_id = str(result.get('new_transaction_id') or '')
        if not command_id.isdigit():
            raise ValueError('DSM nu a confirmat ID-ul comenzii; nu repet automat crearea.')
    order = await call('/special/transaction/get_transaction/' + command_id)
    h = order.get('transaction') or {}
    if str(h.get('transaction_head__type')) != '2' or str(h.get('car_body_series', '')).upper() != VIN or str(h.get('transaction_head__parent')) != OFFER:
        raise ValueError('Verificarea comenzii DSM a eșuat; nu se creează alta.')
    marker = '[ALBATROS PRE-RECEPTIE TEST OFERTA-3415]'
    description = str(h.get('transaction_head__description') or '')
    if marker not in description:
        note = (f'\n{marker}\nSimulare fără rezervare în programator. Comandă reală pentru anulare ulterioară.\n'
                f'Client confirmat: {data["client"]}\nTelefon confirmat: {data["phone"]}\n'
                f'ITP din talon: {data["itp"]}\nCerințe suplimentare: {data.get("extra", "Nu")}\n'
                'Datele au fost confirmate pe WhatsApp. Piesele și manopera provin din oferta semnată.')
        result = await call('/special/transaction/update_transaction_details', {'transaction_id': command_id, 'car_id': str(h['car_id']), 'transaction_head__description': description + note})
        if str(result.get('status')) not in {'1', 'True', 'true'}:
            raise ValueError('Comanda există, dar marcarea TEST necesită verificare.')
        check = await call('/special/transaction/get_transaction/' + command_id)
        if marker not in str((check.get('transaction') or {}).get('transaction_head__description', '')):
            raise ValueError('Comanda există, dar mențiunea TEST nu a fost confirmată.')
    return {'id': command_id, 'code': str(h.get('transaction_head__code') or command_id), 'test': True}
