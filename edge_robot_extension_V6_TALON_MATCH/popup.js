const o=document.getElementById("out");
document.getElementById("go").onclick=async()=>{
  o.textContent="Verific...";
  try{
    const tabs=await chrome.tabs.query({});
    const ic=tabs.filter(t=>{
      try{return new URL(t.url||"").hostname==="ro.e-cat.intercars.eu"}catch(e){return false}
    });
    const st=await chrome.storage.local.get(["currentJob","robotStage","robotTabId"]);
    o.textContent=JSON.stringify({
      version:"V3 MANUAL",
      intercars_tabs:ic.map(t=>({id:t.id,url:t.url,active:t.active})),
      state:st,
      instruction:"Pornirea se face din Dashboard."
    },null,2);
  }catch(e){o.textContent=String(e)}
};