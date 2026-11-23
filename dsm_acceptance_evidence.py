"""Read business-date evidence without exposing customer or vehicle identity."""
import json
import re
from dsm_accepted_offers import offer_record, signature_status, acceptance_datetime
from dsm_open_orders_readonly import _evaluate


async def lookup_registration(registration, appointment_id=None):
    """Read signatures; appointment_id is DSM's internal transaction ID, not APP code."""
    from dsm_readonly import _search_transactions, normalize_registration
    from dsm_booking_reconciliation import read_offer_details, read_appointment, plate
    registration = normalize_registration(registration)
    appointment = None
    if appointment_id is not None:
        if not str(appointment_id).isdigit():
            raise ValueError('Cod de programare invalid.')
        appointment = await read_appointment(str(appointment_id))
        if (plate(appointment.get('carNumber')) != registration
                or not re.fullmatch(r'APP-\d+', str(appointment.get('code') or ''))
                or not str(appointment.get('carId') or '').isdigit() or int(appointment['carId']) <= 1):
            raise ValueError('Programarea nu corespunde înmatriculării cerute.')
    refs = await _search_transactions(registration)
    ids = {str(r.get('transaction_id') or '') for r in refs
           if str(r.get('transaction_type')) == '1'}
    if appointment and str(appointment.get('estimateType')) in {'1', '2'}:
        ids.add(str(appointment.get('estimateId') or ''))
    ids.discard('')
    if len(ids) > 100 or any(not ident.isdigit() for ident in ids):
        raise ValueError('Istoricul ofertelor nu poate fi verificat complet.')
    payloads = await read_offer_details(sorted(ids)) if ids else {}
    order_id = str((appointment or {}).get('estimateId') or '')
    linked_order = (payloads.get(order_id) or {}).get('transaction') or {}
    offers = []
    vehicle_ids = set()
    for ident, payload in payloads.items():
        h = payload.get('transaction') or {}
        if str(h.get('transaction_head__type')) != '1' or plate(h.get('car_auto_number')) != registration:
            continue
        if appointment and str(h.get('car_id')) != str(appointment.get('carId')):
            continue
        vehicle_ids.add((str(h.get('car_body_series') or ''), str(h.get('car_id') or '')))
        related = bool(appointment and (
            (str(appointment.get('estimateType')) == '1' and ident == order_id)
            or any(str(c.get('transaction_id')) == order_id and str(c.get('transaction_head__type')) == '2'
                   for c in payload.get('transaction_head_childs') or [])
            or (str(linked_order.get('parent_transaction_type')) == '1'
                and linked_order.get('parent_transaction_code') == h.get('transaction_head__code')
                and str(linked_order.get('car_id')) == str(h.get('car_id')))))
        signature = signature_status(payload)
        offers.append(dict(offer_id=ident, offer_code=h.get('transaction_head__code'),
            document_status=h.get('transaction_head__status'), signature=signature,
            signed=signature in {'signed', 'complete', 'completed'},
            accepted_at=acceptance_datetime(payload), linked_to_appointment=related))
    return dict(registration=registration, offers=offers, ambiguous_vehicle=len(vehicle_ids) > 1,
                appointment_id=appointment.get('code') if appointment else None)


async def inspect(db_path, request_id):
    record = offer_record(db_path, request_id)
    if not record or not str(record['transaction_id']).isdigit():
        raise ValueError('Cererea nu are o ofertă DSM asociată.')
    ident = str(record['transaction_id'])
    result = await _evaluate(r'''(async()=>{
      const id=__ID__;
      const r=await fetch('/special/transaction/get_transaction/'+id,{credentials:'same-origin',signal:AbortSignal.timeout(12000)});
      if(!r.ok||r.url.includes('/login'))throw Error('Citire DSM indisponibila');
      const p=await r.json();if(String(p.transaction?.transaction_head__id)!==id)throw Error('Oferta neconcordanta');
      const keys=v=>v&&typeof v==='object'?Object.keys(v).filter(k=>/^[a-zA-Z_][a-zA-Z0-9_]{0,79}$/.test(k)):[];
      const dates=[];
      const walk=(v,path,depth)=>{
        if(!v||typeof v!=='object'||depth>4||dates.length>=100)return;
        if(Array.isArray(v)){for(const item of v.slice(0,8))walk(item,path+'[].',depth+1);return;}
        for(const k of keys(v)){
          const value=v[k];
          if(/signed|accept|signature|sms.*date|feedback.*date|record_(insert|update)__datetime|^transaction_head__date$/i.test(k)
             &&typeof value==='string'&&/^(?:20\d{2}[-/.]\d{2}[-/.]\d{2}|\d{2}[-/.]\d{2}[-/.]20\d{2})(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$/.test(value)&&value.length<=40)
            dates.push({field:path+k,value});
          if(typeof value==='object')walk(value,path+k+'.',depth+1);
        }
      };
      walk(p,'',0);
      const states={};
      for(const [k,v] of Object.entries(p.sms_or_qrcode_sended||{})){
        if(k.replace(/^\d+_/,'')!=='service_offer')continue;
        const status=typeof v==='string'?v:String(v?.record_status__code||v?.sms_feedback||v?.status||v?.state||'');
        const safe={status};
        for(const field of ['signed_at','accepted_at','signature_datetime','acceptance_datetime'])
          if(typeof v?.[field]==='string'&&/^20\d{2}-\d{2}-\d{2}/.test(v[field])&&v[field].length<=40)safe[field]=v[field];
        states[k]=safe;
      }
      return JSON.stringify({sms_or_qrcode_sended:states,source_fields:keys(p),
        head_fields:keys(p.transaction), document_fields:Array.isArray(p.documents)?p.documents.slice(0,3).map(keys):keys(p.documents),
        signature_fields:Object.entries(p.sms_or_qrcode_sended||{}).filter(([k])=>k.replace(/^\d+_/,'')==='service_offer')
          .map(([k,v])=>({document:k,type:typeof v,fields:keys(v)})),business_dates:dates});
    })()'''.replace('__ID__', json.dumps(ident)))
    return dict(request_id=request_id, offer_code=record['document_code'],
                signature=signature_status(result), accepted_at=acceptance_datetime(result),
                source_fields=result.get('source_fields', []), signature_fields=result.get('signature_fields', []),
                head_fields=result.get('head_fields', []), document_fields=result.get('document_fields', []),
                business_dates=result.get('business_dates', []))
