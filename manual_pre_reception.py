"""Explicit administrator-started document test on one DSM vehicle/test phone."""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from dsm_readonly import lookup_vehicle_async
from dsm_pre_arrival import existing_orders, check_head
from dsm_pre_reception import (call, vehicle_fields, client_fields,
    save_vehicle_details, save_client_details, requirements_description)


async def seed(registration):
    registration = re.sub(r'[^A-Z0-9]', '', str(registration).upper())
    if not re.fullmatch(r'[A-Z0-9]{3,12}', registration):
        raise ValueError('Înmatriculare invalidă pentru test.')
    vehicle = await lookup_vehicle_async(registration)
    if not vehicle.get('confirmed') or not re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}', str(vehicle.get('vin') or '')):
        raise ValueError('Mașina nu este identificată sigur în DSM.')
    rows = vehicle.get('transactions') or []
    if not rows:
        raise ValueError('Lipsește documentul DSM pentru identificarea clientului.')
    source_id = str(rows[0]['transaction_id'])
    source = await call('/special/transaction/get_transaction/' + source_id)
    head = source.get('transaction') or {}
    data = dict(manual_test=True, new_test_order=True, registration=registration,
                vin=vehicle['vin'], car_id=str(head.get('car_id') or ''),
                contact_id=str(head.get('contact_id') or ''), source_transaction_id=source_id)
    if (str(head.get('car_body_series') or '').upper() != data['vin']
            or re.sub(r'\W', '', str(head.get('car_auto_number') or '')).upper() != registration
            or not all(v.isdigit() and int(v)>1 for v in (data['car_id'], data['contact_id']))):
        raise ValueError('Clientul și mașina documentului DSM nu corespund testului.')
    active = await existing_orders(data)
    if len(active)>1:
        raise ValueError('Există mai multe comenzi active; trebuie aleasă comanda înaintea testului.')
    if active:
        data.update(retest_command_id=active[0]['id'], retest_command_code=active[0]['code'])
    client = await call('/special/contact/get_client', dict(contact_id=data['contact_id'], transaction_id=source_id))
    car = await call('/special/car/get_car', dict(car_id=data['car_id']))
    if str(car.get('car_body_series') or '').upper()!=data['vin']:
        raise ValueError('VIN neconcordant în Date Mașină.')
    mobiles = client.get('mobile') or []
    selected = [m for m in mobiles if m.get('checked')=='checked']
    mobile = selected[0] if len(selected)==1 else mobiles[0] if len(mobiles)==1 else {}
    addresses = client.get('address') or []
    if len(addresses)>1:
        raise ValueError('Clientul are mai multe adrese; verificare înaintea testului.')
    address = addresses[0] if addresses else {}
    previous = dict(client_name=str(client.get('contact_name') or '').strip(),
        client_phone=str(mobile.get('val') or ''), client_address_street=str(address.get('address_street_1') or ''),
        car_manufacture_year=str(car.get('car_manufacture_year') or ''), car_itp_expires_date=str(car.get('car_itp_expires_date') or ''))
    data.update(offer_id='manual:'+registration+':'+data['car_id'], previous_dsm=previous,
        client_name=previous['client_name'], client_address=previous['client_address_street'],
        phone=previous['client_phone'], work='TEST verificare talon și completare date recepție — '+registration)
    return data


async def write(data, allow_create, record_created):
    if (not data.get('manual_test') or not data.get('test_run_id') or not all(data.get(k) for k in (
            'document_verified', 'document_current', 'document_fields_confirmed', 'final_confirmed'))):
        raise ValueError('Talonul și datele finale trebuie confirmate înaintea scrierii.')
    vehicle_fields(data); client_fields(data)
    source = (await call('/special/transaction/get_transaction/'+data['source_transaction_id'])).get('transaction') or {}
    if (str(source.get('car_id'))!=data['car_id'] or str(source.get('contact_id'))!=data['contact_id']
            or str(source.get('car_body_series') or '').upper()!=data['vin']):
        raise ValueError('Identitatea sursă DSM s-a schimbat.')
    active = await existing_orders(data)
    ident = str(data.get('_created_command_id') or data.get('retest_command_id') or '')
    if active:
        if len(active)!=1 or ident and active[0]['id']!=ident:
            raise ValueError('Comenzile existente necesită verificare; nu creez duplicat.')
        ident = active[0]['id']
    if not ident:
        if not allow_create:
            raise ValueError('Creare anterioară incertă; nu repet comanda.')
        moment = datetime.now(ZoneInfo('Europe/Bucharest')).strftime('%Y-%m-%d %H:%M')
        result = await call('/special/transaction/start_transaction', dict(contact_id=data['contact_id'],
            car_id=data['car_id'],transaction_type='2',change_insurance='0',reset_client='0',reset_car='0',
            recalculate_row='0',reset_center='0',transaction_start=moment,transaction_duration='0:00',transaction_agreed_period=moment))
        ident = str(result.get('transaction_id') or '')
        if not ident.isdigit() or int(ident)<=1:
            raise ValueError('DSM nu a confirmat ID-ul comenzii; nu repet crearea.')
        record_created(ident)
    else:
        record_created(ident)
    order = await call('/special/transaction/get_transaction/'+ident)
    head = order.get('transaction') or {}
    if not head.get('car_id'):
        check_head(head,data,ident,empty=True)
        if order.get('transaction_rows'):
            raise ValueError('Comanda fără mașină conține poziții; verificare necesară.')
        await call('/special/transaction/transaction_car', dict(car_id=data['car_id'],transaction_id=ident,contact_id=data['contact_id']))
        head = (await call('/special/transaction/get_transaction/'+ident)).get('transaction') or {}
    check_head(head,data,ident)
    await save_vehicle_details(data,head,ident)
    await save_client_details(data,head,ident)
    description = str(head.get('transaction_head__description') or '')
    desired = requirements_description(data,description)
    marker = '[TEST PRE-RECEPTIE '+data['registration']+']'
    if marker not in desired:
        desired += '\n'+marker
    if desired!=description:
        result=await call('/special/transaction/update_transaction_details',dict(transaction_id=ident,car_id=data['car_id'],transaction_head__description=desired))
        if str(result.get('status')) not in {'1','true','True'}:
            raise ValueError('Cerințele testului nu au fost salvate.')
        check = await call('/special/transaction/get_transaction/'+ident)
        if (check.get('transaction') or {}).get('transaction_head__description')!=desired:
            raise ValueError('Cerințele salvate nu sunt confirmate în DSM.')
    return dict(id=ident,code=str(head.get('transaction_head__code') or ident),test=True,itp=data['itp'],
                itp_source=data.get('itp_source'),registration=data['registration'],fields_verified=True)
