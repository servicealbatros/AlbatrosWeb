"""Appointment-scoped production pre-reception; no pilot vehicle constants."""
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from dsm_billing_sync import _read_details
from dsm_readonly import _search_transactions
from dsm_open_orders_readonly import _evaluate
from dsm_pre_reception import (call, children, client_fields, vehicle_fields,
    save_client_details, save_vehicle_details, requirements_description)
from pre_reception import phone


def plate(value):
    return re.sub(r'[^A-Z0-9]','',str(value or '').upper())


async def appointment_call(action,data):
    if action not in {'getappointment','transformappointment'} or not str(data.get('appointment_id') or '').isdigit():
        raise ValueError('Operație de programare nepermisă.')
    if action=='transformappointment' and (set(data)!={'appointment_id','target_type'} or str(data['target_type'])!='2'):
        raise ValueError('Se permite numai conversia nativă în comandă, fără forțarea unui duplicat.')
    return await _evaluate('''(async()=>{
      if(location.protocol!=='https:'||!['s.servicemanager.q-service.ro','apps.dsmpro.ro','appm.dsmpro.ro'].includes(location.hostname))throw Error('Domeniu DSM nepermis');
      const r=await fetch(__PATH__,{method:'POST',credentials:'same-origin',headers:{Accept:'application/json','Content-Type':'application/x-www-form-urlencoded','X-CSRF-TOKEN':document.querySelector('meta[name="csrf-token"]')?.content||''},body:new URLSearchParams(__DATA__),signal:AbortSignal.timeout(12000)});
      if(!r.ok||r.url.includes('/login'))throw Error('Programarea DSM nu poate fi verificată');return JSON.stringify(await r.json());})()'''
      .replace('__PATH__',json.dumps('/special/appointments/'+action)).replace('__DATA__',json.dumps(data)))


async def read_appointment(ident):
    result=await appointment_call('getappointment',dict(appointment_id=ident))
    detail=result.get('detail') or {}
    if result.get('status')!='success' or not detail.get('carId') or not detail.get('clientId'):
        raise ValueError('Programarea nu are o mașină și un client identificați.')
    car=await call('/special/car/get_car',{'car_id':str(detail['carId'])})
    order_id=str(detail.get('estimateId') or '')
    linked=order_id.isdigit() and int(order_id)>0 and str(detail.get('estimateType'))=='2'
    return dict(transaction=dict(transaction_head__id=str(ident),transaction_head__type='3',
        transaction_head__code=str(detail.get('code') or ''),
        transaction_head__status='completed' if linked else 'working' if detail.get('canCancel') or detail.get('canTransform') else 'inactive',
        car_body_series=str(car.get('car_body_series') or '').upper(),car_auto_number=detail.get('carNumber'),
        car_id=str(detail['carId']),contact_id=str(detail['clientId'])),
        transaction_head_childs=[dict(transaction_id=order_id,transaction_head__type='2',transaction_status='0')] if linked else [],
        appointment_phone=detail.get('clientPhone'),appointment_start=detail.get('start'))


async def seed(appointment):
    registration=plate(appointment.get('registration'))
    if not registration:
        raise ValueError('Lipsește înmatricularea programării; nu trimit către un client presupus.')
    text=str(appointment.get('operations') or '')+' '+str(appointment.get('job_number') or '')
    explicit=set('OFERTA-'+n for n in re.findall(r'\bOFERTA[\s-]+(\d+)\b',text.upper()))
    explicit.update(re.findall(r'\bPHDEV-\d+\b',text.upper()))
    source_id=str(appointment.get('dsm_transaction_id') or '')
    source_kind=str(appointment.get('dsm_transaction_type') or '')
    appointment_phone=appointment.get('phone') or appointment.get('request_phone')
    if not explicit and not appointment_phone and not source_id.isdigit():
        raise ValueError('Programarea nu are telefonul clientului sau o ofertă/comandă asociată explicit; verificare consilier necesară.')
    if source_id.isdigit() and source_kind in {'1','2','3'}:
        payloads={source_id:await read_appointment(source_id)} if source_kind=='3' else await _read_details([source_id])
        if source_kind=='3':
            appointment_phone=payloads[source_id].get('appointment_phone') or appointment_phone
    else:
        refs=await _search_transactions(registration)
        selected=[r for r in refs if str(r.get('transaction_type')) in {'1','2'}
                  and r.get('status') not in {'canceled','cancelled'}
                  and (not explicit or str(r.get('code') or '').upper() in explicit)]
        if not selected or len(selected)>60:
            raise ValueError('Programarea nu poate fi asociată fără ambiguitate cu datele clientului.')
        payloads=await _read_details([str(r['transaction_id']) for r in selected])
    heads=[p['transaction'] for p in payloads.values() if plate(p['transaction'].get('car_auto_number'))==registration]
    groups={(str(h.get('car_body_series') or '').upper(),str(h.get('car_id') or ''),str(h.get('contact_id') or '')) for h in heads}
    if len(groups)!=1 or (explicit and len(heads)!=1):
        raise ValueError('Mai multe mașini, clienți sau documente posibile; consilierul trebuie să confirme destinatarul.')
    vin,car_id,contact_id=groups.pop()
    if not re.fullmatch(r'[A-HJ-NPR-Z0-9]{17}',vin) or not car_id.isdigit() or not contact_id.isdigit() or min(int(car_id),int(contact_id))<=1:
        raise ValueError('VIN-ul, mașina sau clientul nu sunt identificați complet.')
    h=heads[0]
    if source_id.isdigit() and (str(h.get('transaction_head__type'))!=source_kind or h.get('transaction_head__status') in {'canceled','cancelled','billed'}
            or source_kind in {'2','3'} and h.get('transaction_head__status')!='working'):
        raise ValueError('Documentul programării nu mai este activ sau nu corespunde tipului importat.')
    active=[r for r in heads if str(r.get('transaction_head__type'))=='2' and r.get('transaction_head__status')=='working']
    if not explicit:
        h=active[0] if len(active)==1 else heads[0]
    ident=str(h['transaction_head__id'])
    client=await call('/special/contact/get_client',{'contact_id':contact_id,'transaction_id':ident})
    car=await call('/special/car/get_car',{'car_id':car_id})
    if str(car.get('car_body_series') or '').upper()!=vin:
        raise ValueError('VIN neconcordant în datele mașinii.')
    mobiles=client.get('mobile') or []
    checked=[m for m in mobiles if m.get('checked')=='checked']
    matched=[m for m in mobiles if appointment_phone and phone(m.get('val') or '')==phone(appointment_phone)]
    mobile=matched[0] if len(matched)==1 else checked[0] if len(checked)==1 else mobiles[0] if len(mobiles)==1 else None
    target=phone((mobile or {}).get('val') or '')
    if not re.fullmatch(r'407\d{8}',target):
        raise ValueError('Lipsește un număr mobil unic și valid al clientului.')
    if appointment_phone and phone(appointment_phone)!=target:
        raise ValueError('Telefonul din programare diferă de telefonul clientului; verificare necesară.')
    if appointment.get('request_vin') and str(appointment['request_vin']).upper()!=vin:
        raise ValueError('VIN-ul cererii diferă de documentul asociat programării.')
    addresses=client.get('address') or []
    address=addresses[0].get('address_street_1','') if len(addresses)==1 else ''
    previous=dict(client_name=str(client.get('contact_name') or '').strip(),client_phone=target,
        client_address_street=str(address).strip(),car_manufacture_year=str(car.get('car_manufacture_year') or ''),
        car_itp_expires_date=str(car.get('car_itp_expires_date') or ''))
    return dict(vin=vin,registration=registration,phone=target,client_name=previous['client_name'],
        client_address=previous['client_address_street'],previous_dsm=previous,
        work=str(appointment.get('operations') or 'Lucrările programate'),car_id=car_id,contact_id=contact_id,
        source_transaction_id=ident,source_type=str(h['transaction_head__type']),source_code=str(h.get('transaction_head__code') or ''),
        source_explicit=bool(explicit or source_id.isdigit()),requires_order_review=bool(active and not explicit and not source_id.isdigit()),
        appointment_command_id=str(appointment.get('pre_arrival_command_id') or ''),
        source_appointment_start=(payloads.get(source_id) or {}).get('appointment_start',''))


async def _create(path, data):
    if not re.fullmatch(r'/special/transaction/(offer_accepted/\d+|start_transaction)',path):
        raise ValueError('Rută de creare nepermisă.')
    return await _evaluate('''(async()=>{
      if(location.protocol!=='https:'||!['s.servicemanager.q-service.ro','apps.dsmpro.ro','appm.dsmpro.ro'].includes(location.hostname))throw Error('Domeniu DSM nepermis');
      const r=await fetch(__PATH__,{method:'POST',credentials:'same-origin',headers:{Accept:'application/json','Content-Type':'application/x-www-form-urlencoded','X-CSRF-TOKEN':document.querySelector('meta[name="csrf-token"]')?.content||''},body:new URLSearchParams(__DATA__),signal:AbortSignal.timeout(12000)});
      if(!r.ok||r.url.includes('/login'))throw Error('Creare incertă; verificare consilier necesară');return JSON.stringify(await r.json());})()'''
      .replace('__PATH__',json.dumps(path)).replace('__DATA__',json.dumps(data)))


def check_head(head, data, ident, *, kind='2', empty=False):
    if (str(head.get('transaction_head__id'))!=str(ident) or str(head.get('transaction_head__type'))!=kind
        or head.get('transaction_head__status')!='working' or str(head.get('contact_id'))!=data['contact_id']
        or (not empty and (str(head.get('car_body_series') or '').upper()!=data['vin'] or str(head.get('car_id'))!=data['car_id']))):
        raise ValueError('Documentul nu corespunde programării confirmate sau nu mai este în lucru.')


async def create_or_find(data, allow_create, record_created):
    if not data.get('production') or not all(data.get(k) for k in ('final_confirmed','document_verified','document_current','document_fields_confirmed','phone_confirmed')):
        raise ValueError('Datele finale ale clientului nu sunt confirmate.')
    vehicle_fields(data);client_fields(data)
    source_id=data['source_transaction_id']
    source=await read_appointment(source_id) if data['source_type']=='3' else await call('/special/transaction/get_transaction/'+source_id)
    h=source.get('transaction') or {}
    if (str(h.get('transaction_head__id'))!=source_id or str(h.get('transaction_head__type'))!=data['source_type']
            or h.get('transaction_head__status') not in {'working','completed'}
            or str(h.get('car_body_series') or '').upper()!=data['vin'] or str(h.get('car_id'))!=data['car_id']
            or str(h.get('contact_id'))!=data['contact_id']):
        raise ValueError('Datele sursă s-au modificat; este necesară verificarea consilierului.')
    if data['source_type']=='3' and data.get('source_appointment_start') and source.get('appointment_start')!=data['source_appointment_start']:
        raise ValueError('Programarea a fost mutată după confirmarea clientului.')
    if data.get('requires_order_review'):
        raise ValueError('Există o comandă în lucru fără legătură explicită cu programarea; nu creez un duplicat.')
    ident=str(data.get('_created_command_id') or data.get('appointment_command_id') or '')
    if not ident and data['source_explicit'] and data['source_type']=='3':
        ids=children(source)
        if len(ids)>1:
            raise ValueError('Programarea are mai multe comenzi; verificare consilier necesară.')
        if ids:
            ident=ids[0]
        else:
            if h.get('transaction_head__status')!='working':
                raise ValueError('Programarea nu mai este activă.')
            if not allow_create:
                raise ValueError('Creare anterioară incertă; nu repet operația.')
            result=await appointment_call('transformappointment',dict(appointment_id=source_id,target_type='2'))
            if result.get('status')!='success':
                raise ValueError('DSM solicită alegerea unei comenzi existente sau verificarea conversiei; nu forțez un duplicat.')
            match=re.search(r'/special/transaction/index/(\d+)(?:[/?#]|$)',str(result.get('redirect_url') or ''))
            ident=match[1] if match else ''
            if not ident:
                raise ValueError('Conversie incertă, fără ID de comandă; nu repet crearea.')
    if not ident and data['source_explicit'] and data['source_type']=='2':
        check_head(h,data,source_id);ident=source_id
    if not ident and data['source_explicit'] and data['source_type']=='1':
        ids=children(source)
        if len(ids)>1:
            raise ValueError('Oferta are mai multe comenzi; verificare consilier necesară.')
        if ids:
            ident=ids[0]
        else:
            from dsm_accepted_offers import signature_status
            if h.get('transaction_head__status')!='working' or signature_status(source) not in {'signed','complete','completed'}:
                raise ValueError('Oferta nu mai este semnată și activă.')
            if not allow_create:
                raise ValueError('Creare anterioară incertă; nu repet operația.')
            result=await _create('/special/transaction/offer_accepted/'+source_id,
                dict(payment_advance='false',advance_value='',transform_type='estimate',transform_type_appointment='0'))
            ident=str(result.get('new_transaction_id') or '')
    elif not ident:
        if not allow_create:
            raise ValueError('Creare anterioară incertă; nu repet operația.')
        refs=await _search_transactions(data['registration'])
        active=[str(r['transaction_id']) for r in refs if str(r.get('transaction_type'))=='2' and r.get('status')=='working']
        if active:
            raise ValueError('Există o comandă în lucru neasociată explicit; consilierul trebuie să confirme legătura.')
        moment=datetime.now(ZoneInfo('Europe/Bucharest')).strftime('%Y-%m-%d %H:%M')
        result=await _create('/special/transaction/start_transaction',dict(contact_id=data['contact_id'],car_id=data['car_id'],
            transaction_type='2',change_insurance='0',reset_client='0',reset_car='0',recalculate_row='0',reset_center='0',
            transaction_start=moment,transaction_duration='0:00',transaction_agreed_period=data['appointment_start'].replace('T',' ')))
        ident=str(result.get('transaction_id') or '')
    if not ident.isdigit() or ident==source_id and data['source_type']!='2':
        raise ValueError('ID comandă neconfirmat; nu repet crearea.')
    record_created(ident)
    order=await call('/special/transaction/get_transaction/'+ident)
    head=order.get('transaction') or {}
    if not head.get('car_id'):
        check_head(head,data,ident,empty=True)
        if order.get('transaction_rows'):
            raise ValueError('Comanda incompletă conține deja poziții; verificare necesară.')
        await call('/special/transaction/transaction_car',dict(car_id=data['car_id'],transaction_id=ident,contact_id=data['contact_id']))
        order=await call('/special/transaction/get_transaction/'+ident);head=order.get('transaction') or {}
    check_head(head,data,ident)
    if data['source_explicit'] and data['source_type']=='1' and head.get('parent_transaction_code')!=data['source_code']:
        raise ValueError('Comanda nu aparține ofertei confirmate.')
    await save_vehicle_details(data,head,ident)
    await save_client_details(data,head,ident)
    desired=requirements_description(data,str(head.get('transaction_head__description') or ''))
    marker='[ALBATROS PRE-RECEPTIE '+str(data.get('appointment_id') or source_id)+']'
    if (data['source_type']=='3' or not data['source_explicit']) and marker not in desired:
        desired+='\n'+marker
    if desired!=str(head.get('transaction_head__description') or ''):
        result=await call('/special/transaction/update_transaction_details',dict(transaction_id=ident,car_id=data['car_id'],transaction_head__description=desired))
        if str(result.get('status')) not in {'1','true','True'}:
            raise ValueError('Cerințele nu au fost salvate; verificare necesară.')
        check=await call('/special/transaction/get_transaction/'+ident)
        if (check.get('transaction') or {}).get('transaction_head__description')!=desired:
            raise ValueError('Cerințele salvate nu au putut fi verificate.')
    return dict(id=ident,code=str(head.get('transaction_head__code') or ident),itp=data['itp'],test=False)
