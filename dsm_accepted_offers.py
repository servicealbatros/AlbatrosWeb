"""Read-only DSM acceptance evidence and daily dashboard synchronisation."""
from dsm_open_orders_readonly import _evaluate

async def inspect_acceptance():
    return await _evaluate(r'''(async()=>{
      const home=await fetch('/special/home/index',{credentials:'same-origin',signal:AbortSignal.timeout(15000)});
      if(!home.ok||home.url.includes('/login'))throw Error('Autentificare DSM necesară');
      const html=await home.text(),doc=new DOMParser().parseFromString(html,'text/html');
      const links=[...doc.querySelectorAll('a[href]')].filter(a=>/ofert|offer/i.test(a.textContent+' '+a.getAttribute('href'))).map(a=>({label:a.textContent.trim().slice(0,80),path:new URL(a.getAttribute('href'),home.url).pathname}));
      const sources=[...doc.querySelectorAll('script[src]')].map(s=>new URL(s.getAttribute('src'),home.url)).filter(u=>u.origin===location.origin&&/my-app|transaction_head|home\.js/.test(u.pathname)).slice(0,4);
      const scripts=[];
      for(const src of sources){
        const response=await fetch(src.href,{signal:AbortSignal.timeout(15000)});if(!response.ok)continue;
        const text=await response.text();
        const matches=[...text.matchAll(/.{0,100}(?:signature|signed|semnat|offer_accepted|get.transactions|show_completed_transactions|transaction_type_filter).{0,160}/gi)].slice(0,55).map(m=>m[0]);
        scripts.push({path:src.pathname,matches});
      }
      const r=await fetch('/special/home/get-transactions',{method:'POST',credentials:'same-origin',headers:{Accept:'application/json','Content-Type':'application/x-www-form-urlencoded','X-CSRF-TOKEN':document.querySelector('meta[name="csrf-token"]')?.content||''},body:new URLSearchParams({offset:0,limit:100,show_completed_transactions:3}),signal:AbortSignal.timeout(15000)});
      if(!r.ok||r.url.includes('/login'))throw Error('DSM nu este autentificat');
      const p=await r.json(),rows=Array.isArray(p)?p:p.transactions;
      if(!Array.isArray(rows))throw Error('Format DSM necunoscut');
      const flags=rows.map(row=>Object.fromEntries(Object.entries(row).filter(([k])=>/sign|accept|status|type/.test(k)&&!/car_/.test(k))));
      return JSON.stringify({links,scripts,keys:Object.keys(rows[0]||{}),flags,listing_size:rows.length});
    })()''')
