"""Narrow DSM writer for the explicitly authorised B101TST pilot only."""
import json
import re
from datetime import date
from dsm_open_orders_readonly import _evaluate
from dsm_accepted_offers import eligibility

OFFER = '19527'
VIN = 'WBA00000000000002'
PLATE = 'B101TST'
RETEST_COMMAND = '19529'
LEGACY_MARKER = '[ALBATROS PRE-RECEPTIE TEST OFERTA-3415]'
RECHECK_NOTICE = 'CERINȚELE CLIENTULUI TREBUIE REVERIFICATE ȘI CONFIRMATE ÎMPREUNĂ CU CONSILIERUL LA SEMNAREA COMENZII.'


def requirements_description(data, existing=''):
    # Remove only the old generated pilot block; retain existing customer text.
    original = existing.split(LEGACY_MARKER, 1)[0].strip()
    original = original.replace(RECHECK_NOTICE, '').strip()
    items = [original] if original else []
    work = str(data.get('work') or '').strip()
    if work and work not in original:
        items.append(work)
    extra = str(data.get('extra') or '').strip()
    if extra and extra.casefold() not in {'nu', 'nu.', 'nu am', 'fără', 'fara'}:
        line = 'Cerințe suplimentare: ' + extra
        if line not in original:
            items.append(line)
    return '\n'.join([*items, RECHECK_NOTICE])


def vehicle_fields(data):
    fields = {'car_itp_expires_date': date.fromisoformat(data['itp']).isoformat()}
    year = data.get('manufacture_year')
    if year is not None:
        if not re.fullmatch(r'\d{4}', str(year)) or not 1886 <= int(year) <= date.today().year + 1:
            raise ValueError('Anul fabricației confirmat nu este valid.')
        fields['car_manufacture_year'] = str(year)
    return fields


def client_fields(data):
    name = str(data.get('client_name') or '').strip()
    address = str(data.get('client_address') or '').strip()
    phone = re.sub(r'\D', '', str(data.get('phone') or ''))
    if len(phone) == 10 and phone.startswith('07'):
        phone = '4' + phone
    if not 2 <= len(name) <= 200 or not 8 <= len(address) <= 600 or not re.fullmatch(r'407\d{8}', phone):
        raise ValueError('Numele, adresa și telefonul clientului trebuie completate separat și confirmate.')
    return {'client_name': name, 'client_phone': phone, 'client_address_street': address}


def normalize_saved(field, value):
    value = str(value or '').strip()
    if field == 'client_phone':
        value = re.sub(r'\D', '', value)
        if len(value) == 10 and value.startswith('07'):
            value = '4' + value
    return value


def check_previous(data, current, desired):
    for field, value in desired.items():
        baseline = data.get('previous_dsm', {})
        if field in baseline and normalize_saved(field, current.get(field)) not in {
                normalize_saved(field, baseline[field]), normalize_saved(field, value)}:
            raise ValueError('Datele DSM s-au modificat între timp; este necesară o nouă verificare cu clientul.')


async def save_client_details(data, head, command_id):
    fields = client_fields(data)
    contact_id = str(head.get('contact_id') or '')
    if not contact_id.isdigit() or int(contact_id) <= 1:
        raise ValueError('Clientul comenzii nu a fost identificat.')
    query = {'contact_id': contact_id, 'transaction_id': command_id}
    client = await call('/special/contact/get_client', query)
    mobiles = client.get('mobile') or []
    addresses = client.get('address') or []
    # Reuse the existing channel/address IDs so retries cannot add duplicates.
    mobile = next((m for m in mobiles if re.sub(r'\D', '', str(m.get('val') or '')).removeprefix('4') == fields['client_phone'].removeprefix('4')), None)
    if mobile is None:
        mobile = next((m for m in mobiles if m.get('checked') == 'checked'), None)
    if mobile is None and len(mobiles) == 1:
        mobile = mobiles[0]
    if mobile is None and mobiles:
        raise ValueError('Numărul clientului necesită verificare: există mai multe telefoane.')
    if len(addresses) > 1:
        raise ValueError('Adresa clientului necesită verificare: există mai multe adrese.')
    address = addresses[0] if addresses else {}
    current = {'client_name': str(client.get('contact_name') or ''),
               'client_phone': str((mobile or {}).get('val') or ''),
               'client_address_street': str(address.get('address_street_1') or '')}
    check_previous(data, current, fields)
    for field, value in fields.items():
        old = normalize_saved(field, current[field])
        if old == value:
            continue
        record_id = (mobile or {}).get('id', '') if field == 'client_phone' else address.get('address_id', '') if field == 'client_address_street' else ''
        result = await call('/special/contact/update_contact_detail', {
            **query, 'input': field, 'val': value, 'id': str(record_id)})
        if str(result.get('status')) not in {'1', 'True', 'true'} or result.get('validation'):
            raise ValueError('Salvarea datelor clientului necesită verificare.')
    check = await call('/special/contact/get_client', query)
    phones = [re.sub(r'\D', '', str(m.get('val') or '')) for m in check.get('mobile') or []]
    if (str(check.get('contact_name') or '').strip() != fields['client_name']
            or not any(p == fields['client_phone'] or '4' + p == fields['client_phone'] for p in phones)
            or not any(str(a.get('address_street_1') or '').strip() == fields['client_address_street'] for a in check.get('address') or [])):
        raise ValueError('Datele clientului salvate nu au fost confirmate în DSM.')


async def save_vehicle_details(data, head, command_id):
    fields = vehicle_fields(data)
    car_id = str(head['car_id'])
    car = await call('/special/car/get_car', {'car_id': car_id})
    # Verify the record before modifying shared vehicle data.
    if str(car.get('car_body_series', '')).upper() != VIN:
        raise ValueError('Datele mașinii nu corespund VIN-ului confirmat.')
    check_previous(data, car, fields)
    for field, value in fields.items():
        if normalize_saved(field, car.get(field)) == value:
            continue
        result = await call('/special/car/update_car_detail', {
            'car_id': car_id, 'transaction_id': command_id, 'input': field, 'val': value})
        if str(result.get('status')) not in {'1', 'True', 'true'} or result.get('validation'):
            raise ValueError('Salvarea datelor mașinii necesită verificare.')
    check = await call('/special/car/get_car', {'car_id': car_id})
    if str(check.get('car_body_series', '')).upper() != VIN or any(str(check.get(field) or '') != value for field, value in fields.items()):
        raise ValueError('Datele mașinii salvate nu au fost confirmate în DSM.')


async def call(path, data=None):
    # No arbitrary hosts/routes from messages or OCR.
    if not re.fullmatch(r'/special/(transaction/(get_transaction/\d+|offer_accepted/19527|update_transaction_details)|car/(get_car|update_car_detail)|contact/(get_client|update_contact_detail))', path):
        raise ValueError('Operație DSM nepermisă în pilot.')
    if path.endswith('/update_car_detail') and (not data or data.get('input') not in {'car_manufacture_year', 'car_itp_expires_date'}):
        raise ValueError('Rubrică mașină nepermisă în pilot.')
    if path.endswith('/update_contact_detail') and (not data or data.get('input') not in {'client_name', 'client_phone', 'client_address_street'}):
        raise ValueError('Rubrică client nepermisă în pilot.')
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


def check_retest_order(head):
    if (str(head.get('transaction_head__id')) != RETEST_COMMAND
            or str(head.get('transaction_head__type')) != '2'
            or str(head.get('transaction_head__code')) != 'PHDEV-10296'
            or head.get('transaction_head__status') != 'working'
            or str(head.get('car_body_series', '')).upper() != VIN
            or re.sub(r'\W', '', str(head.get('car_auto_number') or '')).upper() != PLATE):
        raise ValueError('Comanda de test PHDEV-10296 nu mai corespunde sau nu este în lucru.')


async def seed(*, retest=False):
    if retest:
        p = await call('/special/transaction/get_transaction/' + RETEST_COMMAND)
        check_retest_order(p.get('transaction') or {})
    else:
        p = await source()
        if eligibility(p) != 'accepted':
            raise ValueError('Oferta nu mai este semnată și în așteptare; verificare necesară.')
    h = p['transaction']
    client = await call('/special/contact/get_client', {'contact_id': str(h['contact_id']), 'transaction_id': str(h.get('transaction_head__id') or OFFER)})
    car = await call('/special/car/get_car', {'car_id': str(h['car_id'])})
    if str(car.get('car_body_series', '')).upper() != VIN:
        raise ValueError('Datele mașinii nu corespund ofertei.')
    mobiles = client.get('mobile') or []
    mobile = next((m for m in mobiles if m.get('checked') == 'checked'), None)
    if mobile is None and len(mobiles) == 1:
        mobile = mobiles[0]
    addresses = client.get('address') or []
    address = addresses[0] if len(addresses) == 1 else {}
    previous = {'client_name': str(client.get('contact_name') or '').strip(),
                'client_phone': normalize_saved('client_phone', (mobile or {}).get('val')),
                'client_address_street': str(address.get('address_street_1') or '').strip(),
                'car_manufacture_year': str(car.get('car_manufacture_year') or '').strip(),
                'car_itp_expires_date': str(car.get('car_itp_expires_date') or '').strip()}
    data = {'offer_id': OFFER, 'registration': PLATE, 'vin': VIN,
            'client_name': previous['client_name'], 'client': previous['client_name'],
            'phone': previous['client_phone'], 'client_address': previous['client_address_street'],
            'previous_dsm': previous,
            'work': str(h.get('transaction_head__description') or '') or '; '.join(str(r.get('transaction_row__name', '')) for r in p.get('transaction_rows', [])),
            'source_code': str(h.get('transaction_head__code') or OFFER)}
    if retest:
        # The existing command already contains the approved work and recheck
        # notice. Keep those customer requirements separate from intake data.
        data['work'] = str(h.get('transaction_head__description') or '').replace(RECHECK_NOTICE, '').strip()
        data['retest_command_id'] = RETEST_COMMAND
        data['retest_command_code'] = 'PHDEV-10296'
    return data


async def create_or_find(data, allow_create):
    if data.get('vin') != VIN or data.get('registration') != PLATE or not data.get('final_confirmed') or not data.get('document_verified') or not data.get('document_current') or not data.get('document_fields_confirmed'):
        raise ValueError('Datele finale ale pilotului nu sunt confirmate.')
    vehicle_fields(data)  # Validate before creating a real order.
    client_fields(data)
    retest_id = data.get('retest_command_id')
    if retest_id:
        if str(retest_id) != RETEST_COMMAND:
            raise ValueError('Comandă nepermisă pentru reluarea testului.')
        command_id = RETEST_COMMAND
    else:
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
    if retest_id:
        check_retest_order(h)
    elif str(h.get('transaction_head__type')) != '2' or str(h.get('car_body_series', '')).upper() != VIN or str(h.get('transaction_head__parent')) != OFFER:
        raise ValueError('Verificarea comenzii DSM a eșuat; nu se creează alta.')
    await save_vehicle_details(data, h, command_id)
    await save_client_details(data, h, command_id)
    description = str(h.get('transaction_head__description') or '')
    desired = requirements_description(data, description)
    if description != desired:
        result = await call('/special/transaction/update_transaction_details', {'transaction_id': command_id, 'car_id': str(h['car_id']), 'transaction_head__description': desired})
        if str(result.get('status')) not in {'1', 'True', 'true'}:
            raise ValueError('Comanda există, dar cerințele clientului necesită verificare.')
        check = await call('/special/transaction/get_transaction/' + command_id)
        if desired != str((check.get('transaction') or {}).get('transaction_head__description', '')):
            raise ValueError('Comanda există, dar cerințele clientului nu au fost confirmate.')
    return {'id': command_id, 'code': str(h.get('transaction_head__code') or command_id), 'test': True,
            'itp': data['itp'], 'itp_source': data.get('itp_source', 'client')}
