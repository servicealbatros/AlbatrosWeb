"""Read all working DSM work orders through the existing authenticated tab."""
from __future__ import annotations
import asyncio
import json
import re
import websockets
from dsm_readonly import _home_target, _call
from dsm_history_import import READ_DETAIL_JS

class DsmOpenOrdersError(RuntimeError):
    pass

async def _evaluate(expression):
    try:
        target=_home_target()
        if '/login' in target.get('url',''):
            raise DsmOpenOrdersError('Autentificare DSM necesară în fila robotului.')
        async with websockets.connect(target['webSocketDebuggerUrl'],open_timeout=5,close_timeout=2,max_size=16000000) as ws:
            result=await _call(ws,1,'Runtime.evaluate',{'expression':expression,'awaitPromise':True,'returnByValue':True})
        raw=result.get('result',{}).get('result',{}).get('value')
        if not raw:
            details=result.get('result',{}).get('exceptionDetails',{})
            raise DsmOpenOrdersError(str(details.get('exception',{}).get('description') or 'DSM nu a returnat lista. Verifică autentificarea.'))
        return json.loads(raw)
    except DsmOpenOrdersError:
        raise
    except Exception as exc:
        raise DsmOpenOrdersError('Nu pot citi comenzile în lucru: '+str(exc)) from exc

async def _listing_page(offset):
    return await _evaluate(r"""(async()=>{
 const r=await fetch('/special/home/get-transactions',{method:'POST',credentials:'same-origin',headers:{Accept:'application/json','Content-Type':'application/x-www-form-urlencoded','X-CSRF-TOKEN':document.querySelector('meta[name="csrf-token"]')?.content||''},body:new URLSearchParams({offset:__OFFSET__,limit:100,show_completed_transactions:1}),signal:AbortSignal.timeout(15000)});
 if(!r.ok||r.url.includes('/login'))throw Error('Autentificare DSM necesară sau HTTP '+r.status);
 const p=await r.json();if(!Array.isArray(p)&&!Array.isArray(p.transactions))throw Error('Format DSM necunoscut; lista locală este păstrată.');return JSON.stringify({rows:Array.isArray(p)?p:p.transactions,limit:p.limit||100});})()""".replace('__OFFSET__',str(offset)))

async def _open_order_refs():
    offset=0;seen=set();refs=[]
    while True:
        page=await _listing_page(offset)
        rows=page.get('rows')
        if not isinstance(rows,list):
            raise DsmOpenOrdersError('Lista DSM are un format necunoscut; lista locală rămâne păstrată.')
        fresh=[r for r in rows if str(r.get('transaction_id') or r.get('id') or '') not in seen]
        if rows and not fresh:
            raise DsmOpenOrdersError('DSM repetă pagina; actualizarea nu este completă.')
        for row in fresh:
            ident=str(row.get('transaction_id') or row.get('id') or '')
            seen.add(ident)
            status=str(row.get('status') or row.get('transaction_head__status') or '').lower()
            code=str(row.get('code') or row.get('transaction_head__code') or '').strip()
            registration=str(row.get('car_no') or row.get('car_number') or '').strip().upper()
            if not ident.isdigit() or status!='working' or not re.fullmatch(r'(?:PHDEV|ALB)-\d+',code,re.I):
                continue
            if not registration or re.search(r'FARA\s*MASINA',registration):
                continue
            refs.append({'transaction_id':ident,'job_number':code,'registration':registration,
                         'opened_at':row.get('transaction_head__date') or row.get('record_date') or '',
                         'vehicle':row.get('car') or '', 'customer_name':row.get('contact_name') or '',
                         'operations':row.get('description') or row.get('transaction_head__description') or ''})
        if len(rows)<int(page.get('limit') or 100):
            break
        offset+=len(rows)
    return refs

async def read_open_work_orders():
    refs=await _open_order_refs()
    orders=[]
    for offset in range(0,len(refs),4):
        chunk=refs[offset:offset+4]
        values=await _evaluate('('+READ_DETAIL_JS+')('+json.dumps([r['transaction_id'] for r in chunk])+')')
        details={str(v.get('detail',{}).get('transaction_id')):v.get('detail') for v in values if v.get('detail')}
        errors=[v.get('error') for v in values if v.get('error')]
        if errors or len(details)!=len(chunk):
            raise DsmOpenOrdersError('Citire incompletă a comenzilor DSM; lista locală nu a fost modificată. '+str(errors[0] if errors else 'Lipsește un deviz.'))
        for ref in chunk:
            detail=details[ref['transaction_id']]
            if detail.get('status')!='working':
                continue
            registration=str(detail.get('registration') or ref['registration']).strip().upper()
            if not registration or re.search(r'FARA\s*MASINA',registration):
                continue
            operations=detail.get('description') or ref['operations'] or '; '.join(str(r.get('name') or '') for r in detail.get('labors') or [])
            orders.append({**ref,'status':'open','vin':detail.get('vin') or '',
                           'registration':registration,'vehicle':detail.get('vehicle') or ref['vehicle'],
                           'customer_name':detail.get('customer_name') or ref['customer_name'],
                           'phone':detail.get('customer_phone') or '',
                           'operations':operations or 'Constatare / comandă DSM deschisă',
                           'has_appointment':False})
    return orders
