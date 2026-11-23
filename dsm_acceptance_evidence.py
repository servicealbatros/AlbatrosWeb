"""Read business-date evidence without exposing customer or vehicle identity."""
import json
from dsm_accepted_offers import offer_record, signature_status, acceptance_datetime
from dsm_open_orders_readonly import _evaluate


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
             &&typeof value==='string'&&/^20\d{2}[-/.]\d{2}[-/.]\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$/.test(value)&&value.length<=40)
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
        signature_fields:Object.entries(p.sms_or_qrcode_sended||{}).filter(([k])=>k.replace(/^\d+_/,'')==='service_offer')
          .map(([k,v])=>({document:k,type:typeof v,fields:keys(v)})),business_dates:dates});
    })()'''.replace('__ID__', json.dumps(ident)))
    return dict(request_id=request_id, offer_code=record['document_code'],
                signature=signature_status(result), accepted_at=acceptance_datetime(result),
                source_fields=result.get('source_fields', []), signature_fields=result.get('signature_fields', []),
                business_dates=result.get('business_dates', []))
