"""Resumable, read-only import of all DSM transactions into local SQLite."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import time
from datetime import datetime
import websockets
from dsm_readonly import _home_target, _call, _targets, DsmReadOnlyError
from urllib.request import urlopen
from dsm_history_cache import DEFAULT_DB, DATA, connect, state, store_headers, store_detail

READ_DETAIL_JS=r"""async(ids)=>{
 const csrf=document.querySelector('meta[name="csrf-token"]')?.content||'';
 const read=async(url,data)=>{
  const response=await fetch(url,{method:data?'POST':'GET',credentials:'same-origin',headers:{Accept:'application/json',...(data?{'Content-Type':'application/x-www-form-urlencoded'}:{}),'X-CSRF-TOKEN':csrf},...(data?{body:new URLSearchParams(data)}:{}),signal:AbortSignal.timeout(15000)});
  if(response.url.includes('/login')||response.status===401||response.status===419)throw Error('Autentificare DSM necesară');
  if(!response.ok)throw Error('DSM HTTP '+response.status);
  if(!(response.headers.get('content-type')||'').includes('json'))throw Error('DSM nu a returnat JSON; verificați autentificarea');
  return response.json();
 };
 const carCache=globalThis.__albatrosDsmCarReads ||= {};
 const clientCache=globalThis.__albatrosDsmClientReads ||= {};
 const output=[];
 const queue=[...ids];
 const work=async()=>{while(queue.length){
  const id=queue.shift();
  try{
   const p=await read('/special/transaction/get_transaction/'+id);
   const h=p.transaction||{};
   if(String(h.transaction_head__id||'')!==String(id))throw Error('ID deviz neconcordant');
   const carId=String(h.car_id||''),contactId=String(h.contact_id||'');
   const [car,client]=await Promise.all([
    carId?(carCache[carId]||(carCache[carId]=await read('/special/car/get_car',{car_id:carId}))):{},
    contactId?(clientCache[contactId]||(clientCache[contactId]=await read('/special/contact/get_client',{contact_id:contactId,transaction_id:id}))):{}
   ]);
   const rows=(p.transaction_rows||[]).map(row=>({row_id:String(row.transaction_row__id||''),kind:row.transaction_row__type==='P'?'part':row.transaction_row__type==='W'?'labor':String(row.transaction_row__type||'other'),code:String(row.transaction_row__code||''),manufacturer:String(row.transaction_row__manufacturer||''),name:String(row.transaction_row__name||''),quantity:Number(row.transaction_row__qty__line||0),unit:String(row.transaction_row__stock__id__uom||''),hours:row.transaction_row__type==='W'?Number(row.transaction_row__qty__line||0):null,historical_price_ron:Number(row.transaction_row__price_sale_p_uom_v_vat||0)}));
   const phones=Array.isArray(client.mobile)?client.mobile:[];
   const phone=phones.find(x=>x?.checked==='checked'&&x.val)||phones.find(x=>x?.val)||{};
   const detail={transaction_id:String(id),document_code:String(h.transaction_head__code||''),registration:String(h.car_auto_number||car.car_auto_number||''),vin:String(h.car_body_series||'').toUpperCase(),registered_car_vin:String(car.car_body_series||'').toUpperCase(),vehicle:[h.car_brand__name||car.car_brand__name,h.car_model__name||car.car_model__name].filter(Boolean).map(x=>String(x).trim()).join(' '),dsm_car_id:carId,customer_name:String(client.contact_name||h.contact_name||''),customer_phone:String(phone.val||''),year:Number(car.car_manufacture_year||0)||null,engine_or_variant:String(car.car_engine||''),power_kw:Number(car.car_horsepower||0)||null,displacement_cc:Number(car.car_engine_capacity||0)||null,mileage_km:Number(h.transaction_car_km||0)||null,service_date:String(h.transaction_head__datetime_appointment||''),description:String(h.transaction_head__description||''),status:String(h.transaction_head__status||''),document_type:String(h.transaction_head__type||''),haynes_submodel_id:h.car_haynes_submodel_id||null,tecrmi_type_id:h.car_tecrmi_type_id||null,tecdoc_type_id:h.car_tecdoc_type_id||null,parts:rows.filter(r=>r.kind==='part'),labors:rows.filter(r=>r.kind==='labor'),other_rows:rows.filter(r=>!['part','labor'].includes(r.kind))};
   output.push({detail});
  }catch(e){output.push({id:String(id),error:String(e.message||e)})}
 }};
 await Promise.all([work(),work()]);
 return JSON.stringify(output);
}"""

_recovery_used = False


async def evaluate(expression):
 global _recovery_used
 for attempt in range(2):
  target=_home_target()
  try:
   async with websockets.connect(target['webSocketDebuggerUrl'],open_timeout=5,close_timeout=2,max_size=16000000) as ws:
    result=await _call(ws,1,'Runtime.evaluate',{'expression':expression,'awaitPromise':True,'returnByValue':True})
    raw=result.get('result',{}).get('result',{}).get('value')
    if not raw:
     details=result.get('result',{}).get('exceptionDetails',{})
     raise RuntimeError('DSM nu a returnat date: '+str(details.get('exception',{}).get('description') or details.get('text') or 'Verifică autentificarea.'))
    return json.loads(raw)
  except DsmReadOnlyError as exc:
   if _recovery_used or attempt or '20 de secunde' not in str(exc):raise
   live=next((t for t in _targets() if t.get('id')==target['id']),{})
   if '/special/home/' not in str(live.get('url') or ''):raise
   check_enabled()
   _recovery_used=True
   state(last_error='Fila DSM blocată; recuperare unică pe același domeniu.')
   browser=json.load(urlopen('http://127.0.0.1:9222/json/version',timeout=3))['webSocketDebuggerUrl']
   async with websockets.connect(browser,open_timeout=5,close_timeout=2) as ws:
    await _call(ws,20,'Target.closeTarget',{'targetId':target['id']})
    await _call(ws,21,'Target.createTarget',{'url':live['url'],'background':True})
   await asyncio.sleep(2)


def check_enabled():
 p=DATA/'robot_enabled.state.json'
 if p.exists() and not json.loads(p.read_text()).get('enabled'):
  raise RuntimeError('Import suspendat de STOP SAFE.')

async def inventory_page(offset):
 expression=r"""(async()=>{
 const r=await fetch('/special/home/get-transactions',{method:'POST',credentials:'same-origin',headers:{Accept:'application/json','Content-Type':'application/x-www-form-urlencoded','X-CSRF-TOKEN':document.querySelector('meta[name="csrf-token"]')?.content||''},body:new URLSearchParams({offset:__OFFSET__,limit:100,show_completed_transactions:3}),signal:AbortSignal.timeout(15000)});
 if(!r.ok||r.url.includes('/login'))throw Error('Autentificare DSM necesară sau HTTP '+r.status);
 const p=await r.json();return JSON.stringify({rows:Array.isArray(p)?p:p.transactions||[],limit:p.limit||100});})()""".replace('__OFFSET__',str(offset))
 return await evaluate(expression)

async def run():
 info=state()
 if info.get('phase')=='complete':
  info=state(phase='inventory',inventory_offset=0,inventory_complete=False)
 with connect() as db:
  db.execute('UPDATE transactions SET error=NULL WHERE complete=0')
 state(phase='inventory' if not info.get('inventory_complete') else 'details',pid=os.getpid(),started_at=datetime.now().isoformat(),last_error=None)
 try:
  if not info.get('inventory_complete'):
   offset=int(info.get('inventory_offset') or 0)
   while True:
    check_enabled();page=await inventory_page(offset);rows=page['rows']
    if not rows:break
    with connect() as db:
     old={r[0] for r in db.execute('SELECT id FROM transactions')}
    if offset and all(str(row.get('transaction_id') or row.get('id')) in old for row in rows):
     # At a resumed offset older headers may exist; check consecutive page signatures instead.
     if state().get('last_page_ids')==[str(row.get('transaction_id') or row.get('id')) for row in rows]:
      raise RuntimeError('DSM repetă pagina; importul a fost oprit fără a declara completitudine.')
    store_headers(rows)
    offset+=len(rows)
    state(inventory_offset=offset,last_page_ids=[str(row.get('transaction_id') or row.get('id')) for row in rows],updated_at=datetime.now().isoformat())
    if len(rows)<int(page['limit']):break
    await asyncio.sleep(.1)
   state(inventory_complete=True,phase='details')
  while True:
   check_enabled()
   with connect() as db:
    ids=[r[0] for r in db.execute('SELECT id FROM transactions WHERE complete=0 AND error IS NULL ORDER BY CAST(id AS INTEGER) DESC LIMIT 4')]
   if not ids:break
   values=await evaluate('('+READ_DETAIL_JS+')('+json.dumps(ids)+')')
   for value in values:
    if value.get('detail'):
     store_detail(value['detail'])
    else:
     error=value.get('error') or 'Eroare de citire'
     if 'Autentificare' in error:raise RuntimeError(error)
     with connect() as db:db.execute('UPDATE transactions SET error=? WHERE id=?',(error,value['id']))
   state(updated_at=datetime.now().isoformat())
   await asyncio.sleep(.1)
  result=state();complete=result['total']==result['imported'] and result['errors']==0
  state(phase='complete' if complete else 'partial',finished_at=datetime.now().isoformat(),pid=None)
 except Exception as exc:
  state(phase='paused',last_error=str(exc),pid=None,updated_at=datetime.now().isoformat())
  raise
 finally:
  # Nu lăsăm un PID de import terminat în lista STOP SAFE.
  try:
   import runtime_control as runtime
   rows=runtime._load()
   if any(r.get('name')=='dsm_history_import' and int(r.get('pid') or 0)==os.getpid() for r in rows):
    enabled=json.loads(runtime.STATE_FILE.read_text()).get('robot_enabled',False)
    runtime._save([r for r in rows if not(r.get('name')=='dsm_history_import' and int(r.get('pid') or 0)==os.getpid())],bool(enabled))
  except Exception:
   pass

if __name__=='__main__':
 asyncio.run(run())
