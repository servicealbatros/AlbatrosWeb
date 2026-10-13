
const API="http://127.0.0.1:8010";
const ALLOWED_HOSTS=new Set(["ro.e-cat.intercars.eu"]);
let busy=false;
chrome.alarms.create("albatrosRobot",{periodInMinutes:0.5});

function isAllowedUrl(raw){
  try{
    const u=new URL(raw||"");
    return u.protocol==="https:" && ALLOWED_HOSTS.has(u.hostname.toLowerCase()) && !u.username && !u.password;
  }catch(_){return false;}
}

async function reportBlocked(raw,reason){
  try{await api("/api/security/browser-event",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({url:String(raw||""),result:"blocked",error:reason})})}catch(_){}
}

chrome.runtime.onInstalled.addListener(()=>{
  chrome.alarms.create("albatrosRobot",{periodInMinutes:0.5});
  setTimeout(tick,800);
});
chrome.runtime.onStartup.addListener(()=>{
  chrome.alarms.create("albatrosRobot",{periodInMinutes:0.5});
  setTimeout(tick,1200);
});
chrome.alarms.onAlarm.addListener(a=>{
  if(a.name==="albatrosRobot") heartbeat().finally(tick);
});

chrome.runtime.onMessage.addListener((m,s,reply)=>{
  if(m.action==="tick"){tick().then(reply);return true}
  const handlers={job_progress:progressJob,job_partial:partialJob,job_done:finishJob,job_error:failJob};
  if(handlers[m.action]){
    (async()=>{
      const st=await chrome.storage.local.get(["currentJob"]);
      if(!m.runId||m.runId!==st.currentJob?.id)return {ok:false,stale:true,error:"Rezultat din altă rulare; ignorat."};
      return await handlers[m.action](m.payload);
    })().then(reply).catch(e=>reply({ok:false,error:String(e)}));
    return true;
  }
});

async function clearRunState(job){
 const st=await chrome.storage.local.get(["currentJob"]);
 if(st.currentJob?.id!==job?.id||st.currentJob?.lock_token!==job?.lock_token)return false;
 await chrome.storage.local.remove(["currentJob","robotStage","robotTabId","robotVehicle","robotProducts","robotSearchIndex","robotItemIndex","robotMissingItems","robotHistoryOnly","robotIdentificationWarning","robotRunId","robotPlanFingerprint"]);
 return true;
}

async function progressJob(payload){
  const st=await chrome.storage.local.get(["currentJob"]);
  const job=st.currentJob;
  if(!job)return {ok:false,error:"Lipseste jobul curent"};
  try{
    return await api(`/api/robot/jobs/${job.id}/progress`,{
      method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)
    },job);
  }catch(e){return {ok:false,error:String(e)};}
}

async function partialJob(payload){
  const st=await chrome.storage.local.get(["currentJob"]);
  const job=st.currentJob;
  if(!job)return {ok:false,error:"Lipseste jobul curent"};
  try{
    const result=await api(`/api/robot/jobs/${job.id}/partial`,{
      method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)
    },job);
    await clearRunState(job);
    setTimeout(tick,1000);
    return result;
  }catch(e){return {ok:false,error:String(e)};}
}

async function api(path,opts={},contextJob=null){
  const st=await chrome.storage.local.get(["workerId","currentJob"]);
  if(!st.workerId){
    st.workerId=crypto.randomUUID();
    await chrome.storage.local.set({workerId:st.workerId});
  }
  const headers=new Headers(opts.headers||{});
  headers.set("X-Worker-ID",st.workerId);
  const leaseJob=contextJob||st.currentJob;
  if(leaseJob?.lock_token) headers.set("X-Lock-Token",leaseJob.lock_token);
  const r=await fetch(API+path,{...opts,headers,signal:AbortSignal.timeout(15000)});
  if(!r.ok){
    const error=new Error(`${r.status}: ${await r.text()}`);
    error.status=r.status;
    throw error;
  }
  return r.json();
}

async function heartbeat(){
  const st=await chrome.storage.local.get(["currentJob","robotStage"]);
  if(!st.currentJob) return {ok:true,idle:true};
  try{
    const result=await api(`/api/robot/jobs/${st.currentJob.id}/heartbeat`,{
      method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify({stage:st.robotStage||"working"})
    });
    const latest=await chrome.storage.local.get(["currentJob"]);
    if(latest.currentJob?.id===st.currentJob.id&&latest.currentJob?.lock_token===st.currentJob.lock_token&&result?.vehicle_hints && Object.keys(result.vehicle_hints).length){
      await chrome.storage.local.set({
        currentJob:{...st.currentJob,vehicle_hints:result.vehicle_hints}
      });
    }
    return result;
  }catch(e){
    console.log("ALBATROS HEARTBEAT ERROR:",String(e));
    if(e?.status===409||e?.status===404){
      await clearRunState(st.currentJob);
      setTimeout(tick,500);
    }
    return {ok:false,error:String(e)};
  }
}

async function prepareTabForNewJob(tabId){
  const home="https://ro.e-cat.intercars.eu/ro/";
  const tab=await chrome.tabs.get(tabId);
  const current=new URL(tab.url||home);
  const isCleanHome=current.origin+current.pathname===home;
  if(!isCleanHome){
    await chrome.tabs.update(tabId,{url:home});
    await waitTabComplete(tabId,20000);
  }else{
    // O reîncărcare elimină contextul vechi al content scriptului după o
    // actualizare a extensiei și garantează un DOM curat pentru VIN-ul nou.
    await chrome.tabs.reload(tabId);
    await waitTabComplete(tabId,20000);
  }
  await new Promise(r=>setTimeout(r,600));
}

async function waitTabComplete(tabId,timeout=15000){
  const start=Date.now();
  while(Date.now()-start<timeout){
    try{
      const t=await chrome.tabs.get(tabId);
      if(t.status==="complete") return t;
    }catch(_){}
    await new Promise(r=>setTimeout(r,250));
  }
  return await chrome.tabs.get(tabId);
}

async function ensureTab(timeout=12000){
  const start=Date.now();

  while(Date.now()-start<timeout){
    const all=await chrome.tabs.query({});
    const tabs=all.filter(t=>{
      try{
        return isAllowedUrl(t.url);
      }catch(_){
        return false;
      }
    });

    if(tabs.length){
      // Preferam tabul activ, apoi cel mai recent.
      const active=tabs.find(t=>t.active);
      if(active) return active;
      tabs.sort((a,b)=>(b.lastAccessed||0)-(a.lastAccessed||0));
      return tabs[0];
    }

    await new Promise(r=>setTimeout(r,300));
  }

  throw new Error(
    "INTERCARS_TAB_NOT_READY: Dashboard-ul nu a deschis încă tabul Inter Cars."
  );
}
async function injectAndResume(tabId){
  await waitTabComplete(tabId);

  // Inject explicit pentru cazul in care tabul a fost creat DUPA ce jobul a fost luat.
  try{
    await chrome.scripting.executeScript({
      target:{tabId},
      files:["content.js"]
    });
  }catch(e){
    console.log("ALBATROS inject:",String(e));
  }

  await new Promise(r=>setTimeout(r,500));

  try{
    return await chrome.tabs.sendMessage(tabId,{action:"resume"});
  }catch(e){
    // A doua incercare dupa o scurta asteptare.
    await new Promise(r=>setTimeout(r,900));
    try{
      await chrome.scripting.executeScript({
        target:{tabId},
        files:["content.js"]
      });
    }catch(_){}
    await new Promise(r=>setTimeout(r,300));
    return await chrome.tabs.sendMessage(tabId,{action:"resume"});
  }
}

async function tick(){
  if(busy) return {ok:true,busy:true};
  busy=true;

  try{
    const state=await chrome.storage.local.get(["currentJob","robotTabId"]);

    // Daca jobul este deja luat, fortam reluarea lui in Inter Cars.
    if(state.currentJob){
      const alive=await heartbeat();
      if(!alive?.ok && !alive?.lease_expires_at)return {ok:false,error:alive.error||"Sesiunea jobului a expirat."};
      const tab=await ensureTab();
      await chrome.storage.local.set({robotTabId:tab.id});
      await injectAndResume(tab.id);
      return {ok:true,current:state.currentJob,resumed:true};
    }

    const health=await api("/api/robot/health");

    // Dashboard-ul deschide tabul Inter Cars INAINTE de crearea jobului.
    // Asteptam acel tab; extensia nu creeaza niciodata taburi.
    const tab=await ensureTab();

    const next=await api("/api/robot/next");

    if(!next.job){
      return {ok:true,health,message:"Nu sunt joburi noi."};
    }

    // Fiecare job pornește curat. Câmpurile de mai jos aparțin exclusiv
    // căutării precedente și nu trebuie reutilizate pentru alt VIN.
    await chrome.storage.local.remove([
      "robotVehicle","robotProducts","robotSearchIndex","robotItemIndex","robotMissingItems",
      "robotHistoryOnly","robotIdentificationWarning","robotRunId","robotPlanFingerprint"
    ]);
    await chrome.storage.local.set({
      currentJob:next.job,
      robotStage:"START",
      robotTabId:tab.id
    });

    // Un VIN nou începe întotdeauna din pagina de bază, în aceeași filă.
    // Astfel nu poate moșteni vehiculul sau rezultatele jobului precedent.
    await prepareTabForNewJob(tab.id);
    await injectAndResume(tab.id);

    return {
      ok:true,
      job:next.job,
      tabId:tab.id,
      started:true
    };

  }catch(e){
    console.log("ALBATROS ROBOT TICK ERROR:",String(e));
    return {ok:false,error:String(e)};
  }finally{
    busy=false;
  }
}

async function finishJob(payload){
  const st=await chrome.storage.local.get(["currentJob"]);
  const job=st.currentJob;
  if(!job)return {ok:false,error:"Lipseste jobul curent"};

  try{
    const d=await api(`/api/robot/jobs/${job.id}/complete`,{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload)
    },job);

    await clearRunState(job);

    setTimeout(tick,1000);
    return d;

  }catch(e){
    if(e?.status===409||e?.status===404){
      await clearRunState(job);
      setTimeout(tick,500);
    }
    return {ok:false,error:String(e)};
  }
}

async function failJob(payload){
  const st=await chrome.storage.local.get(["currentJob"]);
  const job=st.currentJob;
  if(!job)return {ok:false,error:"Lipseste jobul curent"};

  try{
    const d=await api(`/api/robot/jobs/${job.id}/error`,{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({message:payload.message||"Eroare robot"})
    },job);

    await clearRunState(job);

    setTimeout(tick,1000);
    return d;

  }catch(e){
    return {ok:false,error:String(e)};
  }
}

// Daca Inter Cars termina o navigare in timp ce avem un job,
// reluam automat exact etapa curenta.
chrome.tabs.onUpdated.addListener(async(tabId,changeInfo,tab)=>{
  const state=await chrome.storage.local.get(["currentJob","robotTabId"]);
  if(state.currentJob && state.robotTabId===tabId && changeInfo.url && !isAllowedUrl(changeInfo.url)){
    await reportBlocked(changeInfo.url,"domain_not_allowlisted_or_redirect");
    try{await chrome.tabs.stop(tabId)}catch(_){}
    await failJob({message:"Navigare blocată: domeniul nu este aprobat."});
    return;
  }
  if(changeInfo.status!=="complete") return;

  try{
    const u=new URL(tab.url||"");
    if(u.hostname!=="ro.e-cat.intercars.eu") return;

    const st=await chrome.storage.local.get(["currentJob"]);
    if(!st.currentJob) return;

    await chrome.storage.local.set({robotTabId:tabId});
    setTimeout(()=>injectAndResume(tabId).catch(()=>{}),500);

  }catch(_){}
});

