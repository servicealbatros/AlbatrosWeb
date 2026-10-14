
(()=> {
if(globalThis.__ALBATROS_IC_V6016__) return;
globalThis.__ALBATROS_IC_V6016__=true;

const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const clean=s=>String(s||"").replace(/\s+/g," ").trim();
const vis=e=>{if(!e)return false;let r=e.getBoundingClientRect(),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.display!=="none"&&s.visibility!=="hidden"};
async function waitFor(fn,timeout=15000,step=250){let st=Date.now();while(Date.now()-st<timeout){try{let v=fn();if(v)return v}catch(e){}await sleep(step)}return null}
function setVal(el,v){let p=Object.getPrototypeOf(el);let d=Object.getOwnPropertyDescriptor(p,"value");if(d&&d.set)d.set.call(el,v);else el.value=v;el.dispatchEvent(new Event("input",{bubbles:true}));el.dispatchEvent(new Event("change",{bubbles:true}))}
function clickText(rx){
 const all=[...document.querySelectorAll("button,a,[role=button],div")];
 const e=all.find(x=>vis(x)&&rx.test(clean(x.innerText)));
 if(e){e.click();return true} return false;
}
let activeRunId=null;
async function sendJobMessage(message){
 const st=await state();
 if(!activeRunId||st.currentJob?.id!==activeRunId)return {ok:false,stale:true,error:"Rulare înlocuită."};
 return await chrome.runtime.sendMessage({...message,runId:activeRunId});
}
async function state(){return await chrome.storage.local.get(["currentJob","robotStage","robotVehicle"])}
async function setStage(s,extra={}){await chrome.storage.local.set({robotStage:s,...extra})}

async function startVIN(job){
 const vin=String(job?.vin||"").trim().toUpperCase();
 await chrome.storage.local.set({robotHistoryOnly:false,robotIdentificationWarning:null});

 console.log("ALBATROS ROBOT VIN START:", {
   raw: job?.vin,
   normalized: vin,
   request: job?.request_id || job?.id
 });

 if(!vin || vin.length < 11){
   throw new Error("Cererea nu contine un VIN valid.");
 }

 // Rămânem exclusiv pe Inter Cars RO.
 if(!location.href.startsWith("https://ro.e-cat.intercars.eu/ro/")){
   location.href="https://ro.e-cat.intercars.eu/ro/";
   return;
 }

 // 1) Deschide selectorul VEHICUL dacă nu este deja deschis.
 let input=[...document.querySelectorAll("input.vehiclepicker__searchinput")].find(vis);

 if(!input){
   const trigger =
     document.querySelector(".js-global-vehicle-picker-trigger") ||
     [...document.querySelectorAll("button,a,[role=button],div")].find(
       e=>vis(e) &&
       /selectați vehicul|selectati vehicul|vehicul|vehicle/i.test(clean(e.innerText))
     );

   if(trigger){
     trigger.click();
     await sleep(700);
   }

   // 2) Activează modul numai VIN dacă există.
   const onlyVin=await waitFor(()=>document.querySelector("#onlyvins"),3500);
   if(onlyVin && !onlyVin.checked){
     onlyVin.click();
     onlyVin.dispatchEvent(new Event("input",{bubbles:true}));
     onlyVin.dispatchEvent(new Event("change",{bubbles:true}));
     await sleep(300);
   }

   // 3) Găsește câmpul real VIN.
   input=await waitFor(
     ()=>[...document.querySelectorAll("input.vehiclepicker__searchinput,input")].find(
       e=>vis(e) && (
         e.classList.contains("vehiclepicker__searchinput") ||
         /vin|șasiu|sasiu/i.test(
           (e.placeholder||"")+" "+(e.getAttribute("aria-label")||"")
         )
       )
     ),
     10000
   );
 }

 if(!input){
   throw new Error("Nu gasesc campul VIN in Inter Cars.");
 }

 // 4) Curăță și scrie VIN-ul.
 input.focus();
 try{ input.select(); }catch(_){}

 setVal(input,"");
 await sleep(150);
 setVal(input,vin);
 await sleep(350);

 // React poate rescrie valoarea; verificăm și reîncercăm.
 let actual=String(input.value||"").trim().toUpperCase();

 if(actual !== vin){
   input.focus();
   try{ input.select(); }catch(_){}
   setVal(input,vin);
   await sleep(400);
   actual=String(input.value||"").trim().toUpperCase();
 }

 console.log("ALBATROS VIN CHECK:", {
   expected: vin,
   actual: actual
 });

 // 5) Dacă VIN-ul nu este exact în câmp, NU continuăm.
 if(actual !== vin){
   throw new Error(
     "VIN-ul nu a ramas corect in camp. Asteptat: "+vin+" / Gasit: "+actual
   );
 }

 // 6) Abia acum trimitem ENTER complet.
 for(const type of ["keydown","keypress","keyup"]){
   input.dispatchEvent(
     new KeyboardEvent(type,{
       bubbles:true,
       cancelable:true,
       key:"Enter",
       code:"Enter",
       keyCode:13,
       which:13
     })
   );
 }

 // 7) Confirmăm că Inter Cars a trecut la pasul următor.
 const progressed=await waitFor(()=>{
   const body=clean(document.body.innerText);
   return (
     /Specificați metoda de decodare a VIN-ului/i.test(body) ||
     /Specificati metoda de decodare a VIN-ului/i.test(body) ||
     /CAUTARE CU SCHEME OE/i.test(body) ||
     /CAUTA DOAR CU TECDOC/i.test(body) ||
     body.includes(vin) ||
     /vehicle|vehicul/i.test(location.href)
   );
 },8000,200);

 if(!progressed){
   throw new Error("VIN-ul este scris corect, dar Inter Cars nu a trecut la decodare.");
 }

 // 8) Trecem în WAIT_OE numai după confirmare.
 await setStage("WAIT_OE");
 setTimeout(resume,700);
}
async function waitOE(){
 const exactButton=(label)=>{
   const wanted=clean(label).toUpperCase();
   const els=[...document.querySelectorAll("button,a,[role=button],div,span")].filter(vis);

   const exact=els.filter(e=>clean(e.innerText||e.textContent||"").toUpperCase()===wanted);
   if(!exact.length) return null;

   // Textul poate fi intr-un span din interiorul unui buton dezactivat.
   // Intoarcem numai controlul real, vizibil si actionabil.
   const actionable=[];
   for(const element of exact){
     const target=element.matches("button,a,[role=button]")
       ? element
       : element.closest("button,a,[role=button]");
     if(!target || !vis(target)) continue;
     if(target.disabled || target.getAttribute("aria-disabled")==="true") continue;
     if(!actionable.includes(target)) actionable.push(target);
   }

   actionable.sort((a,b)=>{
     const rank=e=>e.tagName==="BUTTON" ? 0 : (e.getAttribute("role")==="button" ? 1 : 2);
     return rank(a)-rank(b);
   });
   return actionable[0]||null;
 };

 const errorVisible=()=>{
   const body=clean(document.body.innerText);
   return /nu am reușit să decodificăm numărul VIN folosind metoda schemelor OE/i.test(body)
       || /nu am reusit sa decodificam numarul VIN folosind metoda schemelor OE/i.test(body)
       || /OE schemas are not available/i.test(body);
 };

 const trustedClickElement=async(el)=>{
   if(!el) return {ok:false,error:"Element lipsa"};
   if(el.disabled || el.getAttribute("aria-disabled")==="true"){
     return {ok:false,error:"Buton dezactivat"};
   }
   try{el.scrollIntoView({block:"center",inline:"center"})}catch(_){}
   await sleep(150);
   el.click();
   return {ok:true};
 };

 const vinMethodDialogVisible=()=>{
   const controls=[
     document.querySelector('[data-testid="SearchVinModal__searchyByoOEProviderButton"]'),
     document.querySelector('[data-testid="SearchVinModal__searchyByoTecdocProviderButton"]')
   ];
   return controls.some(control=>control && vis(control));
 };

 const vinDecodeProgressed=()=>{
   if(vinMethodDialogVisible()) return false;
   if(/\/vehicle\/(external|CAR_[^/?]+)/i.test(location.pathname)) return true;
   if(findVehicleChoiceRows().length>=2) return true;
   return /Date tehnice TecDoc disponibile/i.test(clean(document.body.innerText));
 };

 // Cautarea de piese necesita un vehicul selectat in catalog. Schemele OE
 // pot afisa datele VIN, dar nu fixeaza intotdeauna vehiculul pentru oferta.
 const tecdocDirect=await waitFor(()=>exactButton("CAUTA DOAR CU TECDOC"),8000,200);
 if(tecdocDirect){
   const click=await trustedClickElement(tecdocDirect);
   if(!click?.ok) throw new Error("Nu pot porni identificarea TecDoc.");
   await setStage("WAIT_VEHICLE");
   setTimeout(resume,900);
   return;
 }

 // ---------------------------------------------------------
 // 1) Daca OE a esuat deja, trecem DIRECT la TecDoc.
 // ---------------------------------------------------------
 if(errorVisible()){
   const tecdoc=await waitFor(
     ()=>exactButton("CAUTA DOAR CU TECDOC"),
     8000,
     200
   );

   if(!tecdoc){
     throw new Error("OE a esuat, dar nu gasesc butonul CAUTA DOAR CU TECDOC.");
   }

   console.log("ALBATROS ROBOT: OE esuat -> fallback TECDOC");
   const click=await trustedClickElement(tecdoc);
   console.log("ALBATROS ROBOT: TecDoc click",click);

   const progressed=await waitFor(()=>{
     return vinDecodeProgressed();
   },12000,250);

   if(!progressed){
     throw new Error("Inter Cars nu a trecut mai departe dupa fallback TecDoc.");
   }

   await setStage("WAIT_VEHICLE");
   setTimeout(resume,700);
   return;
 }

 // ---------------------------------------------------------
 // 2) Prima incercare: Scheme OE.
 // ---------------------------------------------------------
 const oe=await waitFor(
   ()=>exactButton("CAUTARE CU SCHEME OE"),
   15000,
   200
 );

 if(!oe){
   // Unele VIN-uri pot ajunge direct in vehicul.
   const body=clean(document.body.innerText);
   if(/Cod motor|Putere|Model|BMW|Audi|Mercedes|Volkswagen|Skoda|Ford|Renault|Dacia/i.test(body)){
     await setStage("WAIT_VEHICLE");
     setTimeout(resume,500);
     return;
   }
   throw new Error("Nu gasesc butonul CAUTARE CU SCHEME OE.");
 }

 console.log("ALBATROS ROBOT: incerc Scheme OE");
 const click=await trustedClickElement(oe);
 console.log("ALBATROS ROBOT: OE click",click);

 // ---------------------------------------------------------
 // 3) Asteptam fie succes, fie mesaj de esec OE.
 // ---------------------------------------------------------
 const outcome=await waitFor(()=>{
   if(errorVisible()) return "OE_FAILED";
   if(vinDecodeProgressed()) return "OE_OK";

   return null;
 },12000,250);

 if(outcome==="OE_FAILED"){
   console.log("ALBATROS ROBOT: Scheme OE nu poate decoda VIN-ul; trec automat la TecDoc.");

   const tecdoc=await waitFor(
     ()=>exactButton("CAUTA DOAR CU TECDOC"),
     8000,
     200
   );

   if(!tecdoc){
     throw new Error("Scheme OE a esuat si butonul TecDoc nu a fost gasit.");
   }

   const tdClick=await trustedClickElement(tecdoc);
   console.log("ALBATROS ROBOT: fallback TecDoc click",tdClick);

   const tdProgress=await waitFor(()=>{
     return vinDecodeProgressed();
   },12000,250);

   if(!tdProgress){
     throw new Error("TecDoc nu a trecut la identificarea vehiculului.");
   }

   await setStage("WAIT_VEHICLE");
   setTimeout(resume,700);
   return;
 }

 if(outcome==="OE_OK"){
   await setStage("WAIT_VEHICLE");
   setTimeout(resume,700);
   return;
 }

 throw new Error("Inter Cars nu a raspuns nici cu succes OE, nici cu eroare OE.");
}
function normalizeVehicleText(v){
 const normalized=clean(String(v||""))
   .normalize("NFD")
   .replace(/[\u0300-\u036f]/g,"")
   .toUpperCase();
 return normalized.replace(/\bVW\b/g,"VOLKSWAGEN");
}

function parseYear(v){
 const m=String(v||"").match(/\b(19|20)\d{2}\b/);
 return m ? Number(m[0]) : null;
}

function rowYearRange(text){
 const years=[...String(text||"").matchAll(/\b(19|20)\d{2}(?:-\d{2})?\b/g)]
   .map(m=>Number(m[0].slice(0,4)));
 if(!years.length)return null;
 return {min:Math.min(...years),max:Math.max(...years)};
}

function importantTokens(v){
 return normalizeVehicleText(v)
   .split(/[^A-Z0-9]+/)
   .filter(x=>x.length>=2)
   .filter(x=>!/^(BMW|AUTO|MODEL|SERIE|SERIA|VARIANTA|MOTOR|AN|ANUL|DIESEL|BENZINA)$/.test(x));
}

function findVehicleChoiceRows(){
 const buttons=[...document.querySelectorAll('[data-test="vis-button"],button,a,[role=button]')]
   .filter(e=>vis(e)&&/^SELECTEAZA$|^SELECTEAZĂ$/i.test(clean(e.innerText||e.textContent||"")));

 const rows=[];
 for(const b of buttons){
   let c=b;
   for(let i=0;i<8&&c;i++,c=c.parentElement){
     const t=clean(c.innerText||"");
     const selectCount=c.querySelectorAll?.('[data-test="vis-button"]').length||0;
     if(t.length>20 && selectCount===1 && /\d{2,3}\s*kW/i.test(t)){
       rows.push({button:b,row:c,text:t});
       break;
     }
   }
 }
 return rows;
}

function scoreVehicleRow(text,hints){
 const T=normalizeVehicleText(text);
 let score=0;
 const reasons=[];

 const make=normalizeVehicleText(hints?.make);
 if(make && T.includes(make)){score+=20;reasons.push("marca")}

 const model=normalizeVehicleText(hints?.model);
 let bodyCompatible=true;
 if(model){
   const rowIsVan=/\bVAN\b/.test(T);
   const wantsVan=/\bVAN\b/.test(model);
   if(rowIsVan===wantsVan){score+=50;reasons.push(wantsVan?"van":"caroserie")}
   else{score-=200;bodyCompatible=false;reasons.push("caroserie-nepotrivita")}
 }

 const year=parseYear(hints?.year);
 const range=rowYearRange(text);
 if(year && range){
   if(year>=range.min && year<=range.max){
     score+=60;reasons.push("an");
   }else{
     score-=70;reasons.push("an-nepotrivit");
   }
 }

 const displacement=Number(hints?.displacement_cc||0);
 const rowDisplacements=[...String(text).matchAll(/(\d{3,4})\s*(?:ccm|cm3|cm³|cmc|cc)\b/gi)]
   .map(m=>Number(m[1]));
 let displacementCompatible=!displacement||!rowDisplacements.length;
 let exactDisplacement=false;
 if(displacement && rowDisplacements.length){
   const delta=Math.min(...rowDisplacements.map(x=>Math.abs(x-displacement)));
   exactDisplacement=delta<=5;
   if(exactDisplacement){
     score+=140;
     displacementCompatible=true;
     reasons.push("cilindree");
   }else{
     score-=260;
     displacementCompatible=false;
     reasons.push("cilindree-nepotrivita");
   }
 }

 const power=Number(hints?.power_kw||0);
 const powerTolerance=Math.max(1,Number(hints?.catalog_power_tolerance_kw||1));
 const rowPower=[...String(text).matchAll(/(\d{2,3})\s*kW/gi)].map(m=>Number(m[1]));
 let powerCompatible=!power||!rowPower.length;
 if(power && rowPower.length){
   const delta=Math.min(...rowPower.map(x=>Math.abs(x-power)));
   if(delta<=powerTolerance){score+=90;powerCompatible=true;reasons.push(delta===0?"putere":"putere-validata-catalog")}
   else{score-=90;reasons.push("putere-nepotrivita")}
 }

 const drivetrain=normalizeVehicleText(hints?.drivetrain);
 const rowIs4x4=/\b(?:4WD|AWD|4X4|4MATIC|4MOTION|XDRIVE)\b/.test(T);
 const rowIs4x2=/\b(?:2WD|4X2|RWD|FWD|SDRIVE)\b/.test(T);
 let drivetrainCompatible=true;
 if(drivetrain==="4X2"){
   if(rowIs4x4){score-=300;drivetrainCompatible=false;reasons.push("tractiune-nepotrivita-4x4")}
   else if(rowIs4x2){score+=120;reasons.push("tractiune-4x2")}
 }else if(drivetrain==="4X4"){
   if(rowIs4x2){score-=300;drivetrainCompatible=false;reasons.push("tractiune-nepotrivita-4x2")}
   else if(rowIs4x4){score+=120;reasons.push("tractiune-4x4")}
 }

 for(const field of ["model","engine_or_variant"]){
   const tokens=importantTokens(hints?.[field]);
   for(const tok of tokens){
     if(T.includes(tok)){
       // platform codes G20/F30/B102 TST are especially valuable.
       const strong=/^[A-Z]\d{2,3}$|^[A-Z]\d[A-Z0-9]{1,4}$/.test(tok);
       score+=strong?35:8;
       reasons.push(tok);
     }
   }
 }

 return {score,reasons,bodyCompatible,powerCompatible,drivetrainCompatible,
   displacementCompatible,rowPower,rowDisplacements};
}

async function trustedVehicleClick(el){
 try{el.scrollIntoView({block:"center",inline:"center"})}catch(_){}
 await sleep(150);
 // Elementul este deja limitat la butonul SELECTEAZĂ din rândul TecDoc
 // validat după marcă/model/an/putere. Nu folosim coordonate sau debugger.
 el.click();
 return {ok:true};
}

async function resolveVehicleChoice(job){
 const rows=findVehicleChoiceRows();
 if(rows.length<2)return false;

 const refreshed=await state();
 const hints=refreshed.currentJob?.vehicle_hints||job.vehicle_hints||{};
 console.log("V6 TALON HINTS:",hints);

 const ranked=rows.map((r,index)=>({
   ...r,
   index,
   ...scoreVehicleRow(r.text,hints)
 })).sort((a,b)=>b.score-a.score);

 console.log("V6 VEHICLE RANKING:",ranked.map(x=>({
   index:x.index,
   score:x.score,
   reasons:x.reasons,
   text:x.text
 })));

 const compatible=ranked.filter(x=>x.bodyCompatible&&x.powerCompatible&&
   x.displacementCompatible&&x.drivetrainCompatible);
 if(!compatible.length){
   throw new Error(
     "Necesita alegere vehicul: Inter Cars nu ofera o varianta cu aceeasi caroserie si putere (toleranta 1 kW)."
   );
 }

 // Variantele incompatibile nu participă la diferența de scor.
 const best=compatible[0];
 const second=compatible[1];

 // Alegem automat doar cand exista dovada suficienta:
 // - scor minim 40
 // - avantaj minim 25 fata de locul 2
 const confident=best && best.bodyCompatible && best.powerCompatible && best.drivetrainCompatible &&
   best.score>=40 && (!second || best.score-second.score>=25);

 if(!confident){
   console.warn("V6 VEHICLE AMBIGUOUS - nu aleg automat.",ranked);
   throw new Error(
     "Necesita alegere vehicul: TecDoc a gasit mai multe variante iar talonul nu diferentiaza suficient de sigur."
   );
 }

 console.log("V6 VEHICLE AUTOSELECT:",{
   score:best.score,
   reasons:best.reasons,
   text:best.text
 });

 await trustedVehicleClick(best.button);

 const gone=await waitFor(
   ()=>findVehicleChoiceRows().length===0,
   10000,
   250
 );

 if(!gone){
   throw new Error("Am ales varianta TecDoc, dar fereastra Selectati vehiculul nu s-a inchis.");
 }

 await sleep(700);
 return true;
}

async function startHistoryCodeLookup(job,reason){
 if(!Array.isArray(job.history_parts)||!job.history_parts.length)throw new Error(reason);
 await setStage("SEARCH_PRODUCT",{
   robotVehicle:{vin:job.vin,hints:job.vehicle_hints||{},url:location.href,
     text:"Căutare globală după codurile montate anterior în DSM"},
   robotSearchIndex:0,robotItemIndex:0,robotProducts:[],robotMissingItems:[],
   robotHistoryOnly:true,robotIdentificationWarning:reason
 });
 if(location.href!=="https://ro.e-cat.intercars.eu/ro/"){
   location.href="https://ro.e-cat.intercars.eu/ro/";
   return;
 }
 setTimeout(resume,300);
}

async function waitVehicle(job){
 const vinMethodControls=[
   document.querySelector('[data-testid="SearchVinModal__searchyByoOEProviderButton"]'),
   document.querySelector('[data-testid="SearchVinModal__searchyByoTecdocProviderButton"]')
 ];
 if(vinMethodControls.some(control=>control && vis(control))){
   await setStage("WAIT_OE");
   setTimeout(resume,200);
   return;
 }
 const decodedTecDocId=new URLSearchParams(location.search).get("tecdocVehicleId");
 if(/\/vehicle\/external/i.test(location.pathname) && /^CAR_\d+$/i.test(decodedTecDocId||"")){
   location.href=`https://ro.e-cat.intercars.eu/ro/vehicle/${decodedTecDocId}?vin=${encodeURIComponent(job.vin)}&vinProvider=BTR5`;
   setTimeout(resume,1200);
   return;
 }
 if(/\/vehicle\/external/i.test(location.pathname) &&
    findVehicleChoiceRows().length<2 &&
    !/Date tehnice TecDoc disponibile/i.test(document.body.innerText)){
   const continueTecDoc=[...document.querySelectorAll("button")].find(e=>
     vis(e) && /Continuați căutarea pentru TecDoc|Continuati cautarea pentru TecDoc/i.test(clean(e.innerText||e.textContent||""))
   );
   if(!continueTecDoc) throw new Error("VIN identificat, dar lipseste continuarea TecDoc.");
   const click=await trustedVehicleClick(continueTecDoc);
   if(!click?.ok) throw new Error("Nu pot continua identificarea TecDoc.");
   const progressed=await waitFor(()=>
     findVehicleChoiceRows().length>=2 ||
     !/\/vehicle\/external/i.test(location.pathname) ||
     /Date tehnice TecDoc disponibile/i.test(document.body.innerText),
     15000,250
   );
   if(!progressed) throw new Error("TecDoc nu a afisat variantele vehiculului.");
 }
 // Daca TecDoc afiseaza mai multe variante, le rezolvam din datele talonului WhatsApp.
 const choices=await waitFor(()=>{
   const rows=findVehicleChoiceRows();
   if(rows.length>=2)return rows;
   const body=clean(document.body.innerText);
   if(/Date tehnice TecDoc disponibile/i.test(body))return "READY";
   if(!/\/vehicle\/external/i.test(location.pathname) &&
      !/Nu a fost selectat niciun vehicul/i.test(body) &&
      (body.includes(job.vin)||/BMW|Audi|Mercedes|Volkswagen|Skoda|Ford|Renault|Dacia/i.test(body)))return "READY";
   return null;
 },15000,250);

 if(!choices){
   throw new Error("VIN-ul nu a ajuns la pagina/selectia vehiculului.");
 }

 const selectedByRobot=Array.isArray(choices);
 if(selectedByRobot){
   try{
     await resolveVehicleChoice(job);
   }catch(error){
     if(Array.isArray(job.history_parts)&&job.history_parts.length){
       await startHistoryCodeLookup(job,String(error.message||error));
       return;
     }
     throw error;
   }
 }

 const vehicleReady=await waitFor(()=>{
   const rows=findVehicleChoiceRows();
   if(rows.length>=2)return null;
   const body=clean(document.body.innerText);
   return !/\/vehicle\/external/i.test(location.pathname) &&
     (/\/vehicle\/CAR_[^/]+/i.test(location.pathname) || (
     !/Nu a fost selectat niciun vehicul|Selectați vehiculul/i.test(body) &&
     /Cod motor|Putere|Model|Capacitate motor/i.test(body)
   ));
 },12000,250);

 if(!vehicleReady){
   throw new Error("TecDoc afiseaza datele VIN, dar vehiculul nu este selectat in catalog. Este necesara selectia explicita a variantei.");
 }

 const vehicle={
   vin:job.vin,
   hints:job.vehicle_hints||{},
   url:location.href,
   text:clean(document.body.innerText).slice(0,1000)
 };

 const expectedModel=normalizeVehicleText(job.vehicle_hints?.model);
 const selectedText=normalizeVehicleText(`${document.title} ${vehicle.text}`);
 if(expectedModel){
   const expectedTokens=expectedModel.split(/[^A-Z0-9]+/).filter(Boolean);
   const identityTokens=expectedTokens.filter(token=>
     /^\d{3,4}$/.test(token) ||
     (token.length>=4 && !/^(MODEL|SERIE|VARIANTA|PEUGEOT|VOLKSWAGEN|MERCEDES|RENAULT|DACIA|SKODA|FORD|BMW|AUDI)$/.test(token))
   );
   if(identityTokens.length && !identityTokens.some(token=>selectedText.includes(token))){
     throw new Error(
       `Necesita alegere vehicul: DSM indica ${job.vehicle_hints.model}, dar Inter Cars a selectat alt model.`
     );
   }
 }

 await setStage("SEARCH_PRODUCT",{robotVehicle:vehicle,robotSearchIndex:0,robotItemIndex:0,robotProducts:[]});
 return searchProduct(job);
}
function historyPartRelevant(part,raw){
 const request=clean(raw);
 const label=clean(part.requested_item||part.catalog_name||"");
 const families=[
  {part:/cap(?:[aă]t)?\s+(?:de\s+)?bar[aă]|cap\s+(?:de\s+)?bielet[aă].*direc[țt]/i,request:/cap(?:[aă]t|ete)?\s+(?:de\s+)?bar[aă]|cap\s+(?:de\s+)?bielet[aă].*direc[țt]/i},
  {part:/articula[țt]ie\s+axial|bar[aă]\s+axial|bielet[aă]\s+(?:axial|direc)/i,request:/bielet[aă]\s+(?:axial|(?:de\s+)?direc)|bar[aă]\s+axial|articula[țt]ie\s+axial/i},
  {part:/etrier/i,request:/etrier/i},
  {part:/pl[aă]cu[țt]/i,request:/pl[aă]cu[țt]/i},
  {part:/\bdisc/i,request:/\bdisc/i},
  {part:/stabiliz|antiruliu/i,request:/bielet[aă].*(?:stabiliz|antiruliu)|(?:stabiliz|antiruliu).*bielet[aă]/i},
  {part:/bascul|bra[țt]\s+(?:de\s+)?suspens/i,request:/bascul|bra[țt]\s+(?:de\s+)?suspens/i},
  {part:/\bpivot\b|rotul/i,request:/\bpivo(?:t|[țt]i)\b|rotul/i},
  {part:/amortiz/i,request:/amortiz/i},
  {part:/filtru/i,request:/revizie|filtru/i},
  {part:/\bulei\b/i,request:/revizie|schimb\s+ulei|\bulei\b/i}
 ];
 const family=families.find(row=>row.part.test(label));
 return !!family && family.request.test(request);
}
function productPlan(job){
 const raw=clean(job.technical_request||job.customer_request);
 const vehicleMake=clean(job.vehicle_hints?.make||"");
 const componentAxles=pattern=>{
  const axes=new Set(), axlePattern=/\b(fa[țt][aă]|anterior|spate|posterior)\b/ig;
  for(const match of raw.matchAll(pattern)){
   const start=match.index, end=start+match[0].length;
   const lo=Math.max(0,start-24), hi=Math.min(raw.length,end+34);
   const candidates=[...raw.slice(lo,hi).matchAll(axlePattern)].map(item=>{
    const absoluteStart=lo+item.index, absoluteEnd=absoluteStart+item[0].length;
    const distance=absoluteEnd<=start?start-absoluteEnd:absoluteStart>=end?absoluteStart-end:0;
    return {distance,axis:/spate|posterior/i.test(item[0])?"SPATE":"FATA"};
   }).sort((a,b)=>a.distance-b.distance);
   if(candidates.length)axes.add(candidates[0].axis);
  }
  return axes;
 };
 const padAxles=componentAxles(/pl[aă]cu[țt]\w*|pastil\w*/ig);
 const discAxles=componentAxles(/\bdiscur\w*|\bdisc\w*/ig);
 const shockAxles=componentAxles(/amortiz\w*/ig);
 const needsWearSensor=padAxles.size>0 && /\b(?:BMW|AUDI|MERCEDES(?:-BENZ)?|IVECO|PEUGEOT)\b/i.test(vehicleMake);
 const wearSensorItem=position=>({
  label:`senzor uzura placute frana ${position==="SPATE"?"spate":"fata"}`,
  queries:[`senzor uzura placute frana ${position==="SPATE"?"spate":"fata"} TEXTAR`,`senzor uzura placute frana ${position==="SPATE"?"spate":"fata"} TRW`,`senzor uzura placute frana ${position==="SPATE"?"spate":"fata"}`],
  match:/(?:senzor|avertizare|martor).*uzur[aă].*pl[aă]cu[țt]|uzur[aă].*pl[aă]cu[țt].*(?:senzor|avertizare|martor)/i,
  reject:/set\s+pl[aă]cu[țt]|pl[aă]cu[țt]e\s+fr[aâ]n[aă](?!.*senzor)/i,
  position
 });
 const addWearSensors=items=>{
  if(!needsWearSensor)return;
  for(const position of padAxles){
   if(!items.some(item=>/senzor.*uzur|avertizare.*uzur/i.test(clean(item.label)) && item.position===position))
    items.push(wearSensorItem(position));
  }
 };
 if(Array.isArray(job.history_parts) && job.history_parts.length){
  const relevantHistory=job.history_parts.filter(part=>{
   if(!historyPartRelevant(part,raw))return false;
   const label=clean(part.requested_item||part.catalog_name||"");
   const position=String(part.position||"").toUpperCase();
   if(/amortiz/i.test(label)){
    if((position==="SPATE"||/spate|posterior/i.test(label))&&!shockAxles.has("SPATE"))return false;
    if((position==="FATA"||/fa[țt][aă]|anterior/i.test(label))&&!shockAxles.has("FATA"))return false;
   }
   if(/pl[aă]cu[țt]/i.test(label)){
    if((position==="SPATE"||/spate|posterior/i.test(label))&&!padAxles.has("SPATE"))return false;
    if((position==="FATA"||/fa[țt][aă]|anterior/i.test(label))&&!padAxles.has("FATA"))return false;
   }
   if(/\bdisc/i.test(label)){
    if((position==="SPATE"||/spate|posterior/i.test(label))&&!discAxles.has("SPATE"))return false;
    if((position==="FATA"||/fa[țt][aă]|anterior/i.test(label))&&!discAxles.has("FATA"))return false;
   }
   return true;
  });
  const familyKey=part=>{const label=clean(part.requested_item||part.catalog_name||"");
   if(/senzor.*uzur|avertizare.*uzur/i.test(label))return "senzor_uzura_placute";
   if(/etrier/i.test(label))return "etrier";
   if(/pl[aă]cu[țt]/i.test(label))return "placute";
   if(/cap(?:[aă]t)?\s+(?:de\s+)?bar[aă]|cap\s+(?:de\s+)?bielet[aă].*direc[țt]/i.test(label))return "cap_bara";
   if(/bascul|bra[țt].*suspens|bra[țt]\s+transversal/i.test(label))return "bascula";
   if(/filtru.*ulei/i.test(label))return "filtru_ulei";
   if(/filtru.*(?:aer|habitaclu|polen|combustibil|motorin)/i.test(label))return label;
   return label;
  };
  const grouped=new Map();
  for(const part of relevantHistory){
   const key=familyKey(part)+":"+String(part.position||part.part_code||"");
   if(!grouped.has(key))grouped.set(key,[]);
   grouped.get(key).push(part);
  }
  const historical=[...grouped.values()].map(parts=>{
   const exactVin=parts.find(part=>/AutoTotal catalog OE după VIN/i.test(String(part.source||"")));
   const part=exactVin||parts[0];
   const codes=[...new Set(parts.map(row=>row.part_code).filter(Boolean))];
   const names=[...new Set(parts.flatMap(row=>[row.catalog_name,row.requested_item]).filter(Boolean))];
   return ({
   label:part.requested_item,
   queries:[...codes,...names],
   match:(()=>{const label=clean(part.requested_item||part.catalog_name||"");
    if(/burduf|protec[țt]ie|tampon limitator/i.test(label))return /burduf|protec[țt]ie|tampon/i;
    if(/rulment/i.test(label))return /rulment/i;
    if(/flan[sș][aă]|suport/i.test(label))return /flan[sș][aă]|suport/i;
   if(/amortiz/i.test(label))return /amortiz/i;
   if(/pivot|rotul/i.test(label))return /pivot|rotul|articula[țt]ie/i;
    if(/cap(?:[aă]t)?\s+(?:de\s+)?bar[aă]|cap\s+(?:de\s+)?bielet[aă].*direc[țt]/i.test(label))return /cap(?:[aă]t)?\s+(?:de\s+)?bar[aă]|articula[țt]ie.*direc[țt]ie/i;
   if(/bielet/i.test(label))return /bielet|tij[aă]/i;
    if(/bascul|bra[țt].*suspens/i.test(label))return /bascul|bra[țt].*suspens/i;
    if(/etrier/i.test(label))return /etrier/i;
    if(/senzor.*uzur|avertizare.*uzur/i.test(label))return /senzor|avertizare/i;
    return /filtru|ulei|disc|pl[aă]cu[țt]/i;})(),
   reject:(()=>{const label=clean(part.requested_item||part.catalog_name||"");
    if(/burduf|protec[țt]ie|tampon limitator/i.test(label))return /rulment|flan[sș][aă]|suport|[sș]urub|carcas[aă]/i;
    if(/rulment/i.test(label))return /burduf|protec[țt]ie|tampon|[sș]urub|carcas[aă]/i;
    if(/flan[sș][aă]|suport/i.test(label))return /burduf|protec[țt]ie|tampon|[sș]urub|carcas[aă]/i;
   if(/amortiz/i.test(label))return /rulment|flan[sș][aă]|suport|protec[țt]ie|praf|burduf|tampon|[sș]urub|carcas[aă]|set\s+repara[țt]ie|\bkit\b|\bchit\b/i;
     if(/disc.*fr[aâ]n/i.test(label))return /pl[aă]cu[țt]|brake\s*pad/i;
     if(/senzor.*uzur|avertizare.*uzur/i.test(label))return /set.*pl[aă]cu[țt]|disc.*fr[aâ]n/i;
     if(/pl[aă]cu[țt]/i.test(label))return /accesorii|montaj|senzor|avertizare|uzur[aă]/i;
    return null;})(),
   exactCode:exactVin?.part_code||part.part_code,
   exactCodes:codes,
   // Când aceeași familie are coduri DSM și OE, marca aparține codului,
   // nu întregii familii. Codul exact este suficient pentru validare.
   expectedBrand:parts.length===1?(canonicalBrand(part.brand)||clean(part.brand)):null,
   historyFallback:true,
   quantity:Number(part.quantity)||1,
   position:exactVin?.position||null,
   side:exactVin?.side||null,
   packLiters:part.pack_liters||null,
   catalogName:part.catalog_name||null
  });});
  if(/revizie|schimb\s+ulei|ulei\s+(?:si|și)\s+filtr/i.test(raw) &&
     !historical.some(item=>/(?:bu[sș]on|[sș]urub).*(?:baie|golire|ulei)/i.test(clean(item.label)))){
   historical.push({label:"buson baie ulei",queries:["buson baie ulei original","surub golire ulei original","surub baie ulei","buson golire ulei"],match:/(?:bu[sș]on|[sș]urub).*(?:baie|golire|ulei)|(?:baie|golire).*(?:bu[sș]on|[sș]urub)/i,reject:/sortiment|trus[aă]|set\b/i,preferOe:true});
  }

  const hasLabel=pattern=>historical.some(item=>pattern.test(clean(item.label)));
  const hasFrontPads=historical.some(item=>/pl[aă]cu[țt]/i.test(clean(item.label)) && item.position==="FATA");
  const hasRearPads=historical.some(item=>/pl[aă]cu[țt]/i.test(clean(item.label)) && item.position==="SPATE");
  const hasFrontShock=historical.some(item=>/amortiz/i.test(clean(item.label)) && item.position==="FATA");
  const hasRearShock=historical.some(item=>/amortiz/i.test(clean(item.label)) && item.position==="SPATE");
  if(/bascul|bra[țt]\s+(?:de\s+)?suspens/i.test(raw) && !hasLabel(/bascul|bra[țt]\s+(?:de\s+)?suspens/i))
   historical.push({label:"bascula suspensie",queries:["bascula suspensie TRW","brat suspensie DELPHI","brat suspensie LEMFORDER","bascula suspensie"],match:/(?:bascul|bra[țt]).*suspens/i});
  if(/etrier.*(?:spate|posterior)|(?:spate|posterior).*etrier/i.test(raw) && !hasLabel(/etrier/i))
   historical.push({label:"etrier frana spate",queries:["etrier frana spate TRW","etrier frana spate TEXTAR","etrier frana spate"],match:/etrier.*fr[aâ]n/i,position:"SPATE"});
  const asksPads=/pl[aă]cu[țt]|pastile/i.test(raw), asksDiscs=/\bdisc/i.test(raw);
  if(asksPads && (padAxles.has("FATA")||!padAxles.size) && !hasFrontPads)
   historical.push({label:"placute frana fata",queries:["placute frana fata TRW","placute frana fata TEXTAR","placute frana fata"],match:/pl[aă]cu[țt].*fr[aâ]n/i,reject:/accesorii|montaj|senzor|avertizare|uzur[aă]/i,position:"FATA"});
  if(asksPads && padAxles.has("SPATE") && !hasRearPads)
   historical.push({label:"placute frana spate",queries:["placute frana spate TRW","placute frana spate TEXTAR","placute frana spate"],match:/pl[aă]cu[țt].*fr[aâ]n/i,reject:/accesorii|montaj|senzor|avertizare|uzur[aă]/i,position:"SPATE"});
  if(asksDiscs && (discAxles.has("FATA")||!discAxles.size) && !hasLabel(/disc.*(?:fa[țt]a|anterior)/i))
    historical.push({label:"disc frana fata",queries:["disc frana fata TRW","disc frana fata TEXTAR","disc frana fata"],match:/disc.*fr[aâ]n/i,reject:/pl[aă]cu[țt]|brake\s*pad/i,position:"FATA"});
  if(asksDiscs && discAxles.has("SPATE") && !hasLabel(/disc.*(?:spate|posterior)/i))
    historical.push({label:"disc frana spate",queries:["disc frana spate TRW","disc frana spate TEXTAR","disc frana spate"],match:/disc.*fr[aâ]n/i,reject:/pl[aă]cu[țt]|brake\s*pad/i,position:"SPATE"});
  if(/bielet[aă].*(?:stabiliz|antiruliu)|(?:stabiliz|antiruliu).*bielet[aă]/i.test(raw) && !hasLabel(/stabiliz|antiruliu/i))
   historical.push({label:"bieleta bara stabilizatoare fata",queries:["Brat/bieleta suspensie, stabilizator","bieleta stabilizatoare fata TRW","bieleta stabilizatoare fata DELPHI","bieleta stabilizatoare fata LEMFORDER","bieleta stabilizatoare fata"],match:/(?:bra[țt]\s*\/\s*bielet[aă].*stabiliz|(?:bielet[aă]|tij[aă]).*(?:stabiliz|antiruliu))/i,position:"FATA"});
  if(shockAxles.has("FATA") && !hasFrontShock)
   historical.push({label:"amortizor fata",queries:["amortizor fata SACHS","amortizor fata BILSTEIN","amortizor fata KYB","amortizor fata"],match:/amortiz/i,reject:/rulment|flan[sș][aă]|suport|protec[țt]ie|praf|burduf|tampon|[sș]urub|carcas[aă]|set\s+repara[țt]ie|\bkit\b|\bchit\b/i,position:"FATA"});
  if(shockAxles.has("SPATE") && !hasRearShock)
   historical.push({label:"amortizor spate",queries:["amortizor spate SACHS","amortizor spate BILSTEIN","amortizor spate KYB","amortizor spate"],match:/amortiz/i,reject:/rulment|flan[sș][aă]|suport|protec[țt]ie|praf|burduf|tampon|[sș]urub|carcas[aă]|set\s+repara[țt]ie|\bkit\b|\bchit\b/i,position:"SPATE"});
  if(/curea.*(?:accesor|alternator|auxiliar)/i.test(raw) && !hasLabel(/curea.*(?:accesor|alternator|auxiliar)/i))
   historical.push({label:"curea accesorii",queries:["curea accesorii","curea alternator","curea auxiliara"],match:/curea.*(?:accesor|alternator|auxiliar)|(?:accesor|alternator|auxiliar).*curea/i});
  addWearSensors(historical);
  if(/cap(?:[aă]t|ete)?\s+(?:de\s+)?bar[aă]/i.test(raw) && !hasLabel(/cap(?:[aă]t)?\s+(?:de\s+)?bar[aă]/i))
   historical.push({label:"cap de bara directie",queries:["cap bara directie TRW","cap bara directie DELPHI","cap bara directie LEMFORDER","cap bara directie"],match:/(?:cap(?:[aă]t)?\s+(?:de\s+)?bar[aă]|articula[țt]ie.*direc[țt]ie)/i});

  return historical.filter(item=>item.exactCode||item.exactCodes?.length);
 }
 const known=[
   {label:"disc frana fata",test:()=>discAxles.has("FATA"),queries:["disc frana fata TRW","disc frana fata TEXTAR","disc frana fata"],match:/disc.*fr[aâ]n/i,reject:/pl[aă]cu[țt]|brake\s*pad/i,position:"FATA"},
   {label:"disc frana spate",test:()=>discAxles.has("SPATE"),queries:["disc frana spate TRW","disc frana spate TEXTAR","disc frana spate"],match:/disc.*fr[aâ]n/i,reject:/pl[aă]cu[țt]|brake\s*pad/i,position:"SPATE"},
  {label:"placute frana fata",test:()=>padAxles.has("FATA"),queries:["placute frana fata TRW","placute frana fata TEXTAR","placute frana fata"],match:/pl[aă]cu[țt].*fr[aâ]n/i,reject:/accesorii|montaj|senzor|avertizare|uzur[aă]/i,position:"FATA"},
  {label:"placute frana spate",test:()=>padAxles.has("SPATE"),queries:["placute frana spate TRW","placute frana spate TEXTAR","placute frana spate"],match:/pl[aă]cu[țt].*fr[aâ]n/i,reject:/accesorii|montaj|senzor|avertizare|uzur[aă]/i,position:"SPATE"},
  {label:"filtru combustibil",test:/filtru(?:l)?\s+(?:de\s+)?(?:combustibil|motorin[aă]|carburant)/i,queries:["filtru combustibil","filtru motorina","filtru carburant"],match:/\bfiltru[,\s]+(?:de\s+)?(?:combustibil|carburant|motorin[aă])\b/i},
  {label:"filtru ulei",test:/filtru(?:l)?\s+(?:de\s+)?ulei/i,queries:["filtru ulei"],match:/\bfiltru[,\s]+(?:de\s+)?ulei\b/i},
  {label:"filtru aer",test:/filtru(?:l)?\s+(?:de\s+)?aer/i,queries:["filtru aer"],match:/\bfiltru[,\s]+(?:de\s+)?aer\b/i,reject:/habitaclu|polen|cabin/i},
  {label:"filtru habitaclu",test:/filtru(?:l)?\s+(?:de\s+)?(?:habitaclu|polen)/i,queries:["filtru habitaclu","filtru polen"],match:/\bfiltru[,\s]+(?:de\s+)?(?:aer\s+)?(?:habitaclu|polen)\b/i},
  {label:"buson baie ulei",test:/revizie|schimb\s+ulei|ulei\s+(?:si|și)\s+filtr/i,queries:["buson baie ulei original","surub golire ulei original","surub baie ulei","buson golire ulei"],match:/(?:bu[sș]on|[sș]urub).*(?:baie|golire|ulei)|(?:baie|golire).*(?:bu[sș]on|[sș]urub)/i,reject:/sortiment|trus[aă]|set\b/i,preferOe:true},
  {label:"pivot suspensie",test:/\bpivo(?:t|ți|ti)\b|rotul[aă]\s+suspensie/i,queries:["pivot suspensie TRW","pivot suspensie DELPHI","pivot suspensie LEMFORDER","pivot suspensie"],match:/(?:pivot|articula[țt]ie.*sarcin[aă]|rotul[aă].*suspensie)/i},
  {label:"cap de bara directie",test:/cap(?:[aă]t|ete)?\s+(?:de\s+)?bar[aă]|cap\s+(?:de\s+)?bielet[aă].*direc[țt]/i,queries:["cap bara directie TRW","cap bara directie DELPHI","cap bara directie LEMFORDER","cap bara directie"],match:/(?:cap(?:[aă]t)?\s+(?:de\s+)?bar[aă]|articula[țt]ie.*direc[țt]ie)/i},
  {label:"bieleta axiala directie",test:/bielet[aă]\s+(?:axial[aă]|(?:de\s+)?direc[țt]ie)|bar[aă]\s+axial[aă]/i,queries:["bieleta axiala TRW","bieleta directie DELPHI","bara axiala LEMFORDER","bieleta axiala"],match:/(?:bielet[aă].*direc[țt]ie|bar[aă].*axial[aă]|articula[țt]ie.*axial[aă])/i},
  {label:"bieleta bara stabilizatoare",test:/bielet[aă].*(?:stabiliz|antiruliu)|(?:stabiliz|antiruliu).*bielet[aă]/i,queries:["Brat/bieleta suspensie, stabilizator","bieleta stabilizatoare TRW","bieleta antiruliu DELPHI","bieleta stabilizatoare LEMFORDER","bieleta stabilizatoare"],match:/(?:bra[țt]\s*\/\s*bielet[aă].*stabiliz|(?:bielet[aă]|tij[aă]).*(?:stabiliz|antiruliu))/i},
  {label:"amortizor fata",test:()=>shockAxles.has("FATA"),queries:["amortizor fata SACHS","amortizor fata BILSTEIN","amortizor fata KYB","amortizor fata"],match:/amortiz/i,reject:/rulment|flan[sș][aă]|suport|protec[țt]ie|praf|burduf|tampon|[sș]urub|carcas[aă]|set\s+repara[țt]ie|\bkit\b|\bchit\b/i,position:"FATA"},
  {label:"amortizor spate",test:()=>shockAxles.has("SPATE"),queries:["amortizor spate SACHS","amortizor spate BILSTEIN","amortizor spate KYB","amortizor spate"],match:/amortiz/i,reject:/rulment|flan[sș][aă]|suport|protec[țt]ie|praf|burduf|tampon|[sș]urub|carcas[aă]|set\s+repara[țt]ie|\bkit\b|\bchit\b/i,position:"SPATE"},
  {label:"curea accesorii",test:/curea.*(?:accesor|alternator|auxiliar)/i,queries:["curea accesorii","curea alternator","curea auxiliara"],match:/curea.*(?:accesor|alternator|auxiliar)|(?:accesor|alternator|auxiliar).*curea/i},
  {label:"bascula suspensie",test:/bascul|bra[țt]\s+(?:de\s+)?suspens/i,queries:["bascula suspensie TRW","brat suspensie DELPHI","brat suspensie LEMFORDER","bascula suspensie"],match:/(?:bascul|bra[țt]).*suspens/i},
  {label:"etrier frana spate",test:/etrier.*(?:spate|posterior)|(?:spate|posterior).*etrier/i,queries:["etrier frana spate TRW","etrier frana spate TEXTAR","etrier frana spate"],match:/etrier.*fr[aâ]n/i,position:"SPATE"},
  {label:"ulei OE",test:/\brevizie\b|schimb\s+ulei|\bulei\s+(?:motor\s+)?(?:OE|original)|\b(?:OE|original)\s+ulei/i,queries:["ulei motor original 1L","ulei motor OE 1L","ulei motor 1L"],match:/\bulei\s+(?:de\s+)?motor\b|\bmotor[oö]l\b/i,requiresOe:true,requiresOneLiter:true,optional:false}
 ];
 const found=known.filter(x=>typeof x.test==="function"?x.test():x.test.test(raw));
 if((/\brevizie\b/i.test(raw) && /\bfiltr/i.test(raw)) ||
    /schimb\s+ulei\s+(?:si|și)\s+(?:toate\s+)?filtre|ulei\s+(?:si|și)\s+(?:toate\s+)?filtre/i.test(raw)){
  const revision=known.filter(x=>/filtru|buson baie ulei|ulei OE/i.test(x.label));
  const combined=[...revision,...found.filter(x=>!revision.includes(x))];
  addWearSensors(combined);
  return combined;
 }
 if(found.length){
  addWearSensors(found);
  return found;
 }
 const short=raw
   .replace(/^.*?\b(?:pentru|de)\s+(?:înlocuire(?:a)?|inlocuire(?:a)?|schimb(?:area)?)\s+/i,"")
   .replace(/^.*?\b(?:pentru|de)\s+(?=\w)/i,"")
   .replace(/[.!?]+$/g,"")
   .trim();
 return [{label:short||raw,queries:[short||raw],match:null}];
}
async function searchProduct(job){
 const mode=await chrome.storage.local.get(["robotHistoryOnly"]);
 const historyOnly=!!mode.robotHistoryOnly;
 if(/\/vehicle\/(?:external|CAR_[^/]+)\/?$/i.test(location.pathname)){
   const offerLink=[...document.querySelectorAll("a,button")].find(e=>
     vis(e) && /MERGEȚI LA OFERTA COMPLETĂ|MERGETI LA OFERTA COMPLETA/i.test(clean(e.innerText||e.textContent||""))
   );
   if(!offerLink) throw new Error("Pagina VIN nu are linkul catre Oferta completa.");
   const offerUrl=new URL(offerLink.href||"/ro/vehicle/full-offer",location.origin);
   if(offerUrl.origin!==location.origin||offerUrl.pathname!=="/ro/vehicle/full-offer")throw new Error("Ruta ofertei Inter Cars nu este sigură.");
   location.assign(offerUrl.href);
   setTimeout(resume,1200);
   return;
 }
 if(!historyOnly && /Nu a fost selectat niciun vehicul/i.test(document.body.innerText)){
   if(Array.isArray(job.history_parts)&&job.history_parts.length)return await startHistoryCodeLookup(job,"Inter Cars nu a păstrat selecția; caut codurile facturate pentru VIN-ul confirmat.");
   throw new Error("Oferta completa nu are vehicul selectat; reiau identificarea TecDoc.");
 }
 const plan=productPlan(job);
 const {robotSearchIndex=0,robotItemIndex=0}=await chrome.storage.local.get(["robotSearchIndex","robotItemIndex"]);
 const currentItem=plan[robotItemIndex];
 const activeQueries=(historyOnly && currentItem?.exactCodes?.length)
   ?currentItem.exactCodes:currentItem?.queries;
 const query=activeQueries?.[robotSearchIndex];
 if(historyOnly&&!currentItem?.exactCode&&!currentItem?.exactCodes?.length){
   await setStage("WAIT_PRODUCTS");
   return await waitProducts(job,(await state()).robotVehicle);
 }
 if(!query) throw new Error("Nu am găsit produse pentru: "+plan.map(x=>x.label).join(", "));
 let inp=await waitFor(()=>[...document.querySelectorAll("input")].find(e=>{
   if(!vis(e))return false;
   const label=(e.placeholder||"")+" "+(e.getAttribute("aria-label")||"");
   return /c[aă]utare produse|search products|search by name|nume, index/i.test(label)
     && !/categorie|category|vin/i.test(label);
 }),8000);
 if(!inp && historyOnly && /\/c\//.test(location.pathname)){
   const resultUrl=new URL(location.href);
   resultUrl.searchParams.set("q",query+":default");
   resultUrl.searchParams.set("initialSearch","true");
   resultUrl.searchParams.delete("page");
   await setStage("WAIT_PRODUCTS");
   location.assign(resultUrl.href);
   return;
 }
 if(!inp) throw new Error("Nu gasesc cautarea de produse.");
 console.log("ALBATROS PRODUCT SEARCH:",query);
 setVal(inp,query); inp.focus(); await sleep(400);
 await setStage("WAIT_PRODUCTS");
 const searchButton=[...document.querySelectorAll("button")].find(e=>
   vis(e) && (
     normalizeVehicleText(e.getAttribute("aria-label"))==="CAUTARE" ||
     normalizeVehicleText(e.innerText||e.textContent)==="CAUTARE" ||
     normalizeVehicleText(e.getAttribute("title"))==="CAUTARE"
   )
 );
 if(searchButton){
   searchButton.click();
 }else{
   inp.dispatchEvent(new KeyboardEvent("keydown",{bubbles:true,key:"Enter",code:"Enter",keyCode:13,which:13}));
 }
 setTimeout(resume,1300);
}
function cardFor(a){
 let c=a;
 for(let i=0;i<16&&c;i++,c=c.parentElement){
  const t=clean(c.innerText);
  const codes=new Set([...t.matchAll(/Cod Inter Cars\s*:\s*([A-Z0-9_-]+)/gi)].map(m=>m[1]));
  const productRefs=new Set([...c.querySelectorAll('a[href*="/p/"]')].map(e=>(e.getAttribute('href')||'').match(/\/p\/([A-Z0-9_-]+)/i)?.[1]).filter(Boolean));
  if((codes.size===1||productRefs.size===1) && /\d+[,.]\d{2}\s*RON/i.test(t))return c;
 }
 return null;
}
const BRAND_RULES={
 filters:["MANN-FILTER","BOSCH","MAHLE","HENGST","PURFLUX"],
 wheels:["TRW","DELPHI","LEMFÖRDER"],
 brakes:["TEXTAR","TRW","OE"],
 belts:["CONTITECH","DAYCO","OE"],
 oil:["OE"]
};
function canonicalBrand(value){
 const v=clean(value).toUpperCase().replace(/[^A-Z0-9]/g,"");
 const names={MANN:"MANN-FILTER",MANNFILTER:"MANN-FILTER",BOSCH:"BOSCH",MAHLE:"MAHLE",KNECHT:"MAHLE",HENGST:"HENGST",PURFLUX:"PURFLUX",TRW:"TRW",DELPHI:"DELPHI",LEMFORDER:"LEMFÖRDER",TEXTAR:"TEXTAR",CASTROL:"CASTROL",CONTITECH:"CONTITECH",CONTINENTAL:"CONTITECH",DAYCO:"DAYCO",BMW:"OE BMW",MINI:"OE BMW",OEBMW:"OE BMW",OEMINI:"OE BMW",ORIGINALBMW:"OE BMW",MERCEDES:"OE MERCEDES",MERCEDESBENZ:"OE MERCEDES",OEMERCEDES:"OE MERCEDES",OEMERCEDESBENZ:"OE MERCEDES",ORIGINALMERCEDES:"OE MERCEDES",VW:"OE VAG",VOLKSWAGEN:"OE VAG",AUDI:"OE VAG",SEAT:"OE VAG",SKODA:"OE VAG",OEVW:"OE VAG",OEVOLKSWAGEN:"OE VAG",OEAUDI:"OE VAG",ORIGINALVAG:"OE VAG"};
 return names[v]||null;
}
function isOeBrand(value){return /^OE(?:\s|$)/i.test(clean(value));}
function oemMatchesVehicle(brand,make){
 if(!isOeBrand(brand))return true;
 const b=clean(brand).toUpperCase().replace(/[^A-Z0-9]/g,"");
 const m=clean(make).toUpperCase().replace(/[^A-Z0-9]/g,"");
 if(b==="OE"||!m)return true;
 if(/BMW|MINI/.test(m))return b==="OEBMW";
 if(/VOLKSWAGEN|VW|AUDI|SEAT|SKODA|CUPRA/.test(m))return b==="OEVAG";
 if(/MERCEDES|SMART/.test(m))return b==="OEMERCEDES";
 return b.includes(m);
}
function brandGroup(label){
 if(/ulei OE/i.test(label))return "oil";
 if(/filtru/i.test(label))return "filters";
 if(/curea.*(?:accesor|alternator|auxiliar)|(?:accesor|alternator|auxiliar).*curea/i.test(label))return "belts";
 if(/fr[aâ]n|disc|pl[aă]cu[țt]/i.test(label))return "brakes";
 if(/suspens|ro[țt]i|direc[țt]|bra[țt]|biel/i.test(label))return "wheels";
 return null;
}
function extract(){
 const anchors=[...document.querySelectorAll('a')].filter(a=>
   /\/p\//i.test(a.getAttribute("href")||"") && /^(?=.*\d)[A-Z0-9][A-Z0-9 ._/-]{3,}$/i.test(clean(a.innerText))
 );
 const out=[],seen=new Set();
 for(const a of anchors){
  const href=a.getAttribute("href")||"";
  const hm=href.match(/\/p\/([A-Z0-9_-]+)/i);
  const card=cardFor(a); if(!card)continue;
  const renderedRange=document.createRange();renderedRange.selectNodeContents(card);
  const renderedRect=renderedRange.getBoundingClientRect();
  if(!vis(card)&&!(renderedRect.width>0&&renderedRect.height>0))continue;
  const t=clean(card.innerText),lines=(card.innerText||"").split(/\n+/).map(clean).filter(Boolean);
  const cm=t.match(/Cod Inter Cars\s*:\s*([A-Z0-9_-]+)/i);
  const ic=(hm&&hm[1])||(cm&&cm[1]); if(!ic||seen.has(ic))continue; seen.add(ic);
  const retail=t.match(/Pre[tțţ] cu am[aă]nuntul\s*(\d{1,4}[,.]\d{2})\s*RON/i);
  const vals=[...t.matchAll(/(\d{1,4}[,.]\d{2})\s*RON/gi)].map(m=>Number(m[1].replace(",",".")));
  const rp=retail?Number(retail[1].replace(",",".")):null;
  const pp=vals.find(v=>rp===null||Math.abs(v-rp)>0.001)??null;
  const stock={}; for(const m of t.matchAll(/\b(R21|R00|R78|B01|HSN|HZA)\b\s*(>?[\d]+)/gi))stock[m[1].toUpperCase()]=m[2];
  const pcode=clean(a.innerText);
  let desc=clean(a.getAttribute("data-clk-listing-standard-item-category"))||null;
  const rawManufacturer=clean(a.getAttribute("data-clk-listing-standard-item-manufacturer"));
  let brand=canonicalBrand(rawManufacturer)||rawManufacturer||null;
  const idx=lines.findIndex(x=>x===pcode);
  const partWords=/filtru|ulei|motor[oö]l|disc|pl[aă]cu[țt]|bu[sș]on|[sș]urub.*(?:baie|golire)|curea|cap(?:[aă]t)?\s+(?:de\s+)?bar[aă]|etrier|bascul|bra[țt].*suspens|amortiz|rulment|burduf|protec[țt]ie|tampon|pivot|rotul|articula[țt]ie|bielet|tij[aă]/i;
  if(!desc && idx>=0)desc=lines.slice(idx+1,idx+7).find(x=>partWords.test(x)&&!/RON|R21|promo|Cod Inter Cars/i.test(x))||null;
  if(!desc)desc=lines.find(x=>partWords.test(x)&&!/RON|R21|promo|Cod Inter Cars/i.test(x))||null;
  if(!brand)brand=lines.map(canonicalBrand).find(Boolean)||null;
  if(!brand){
   const logos=[...card.querySelectorAll("img[alt],img[title]")];
   brand=logos.flatMap(img=>[img.alt,img.title]).map(canonicalBrand).find(Boolean)||null;
  }
  const del=(t.match(/(Astăzi|Astazi|Mâine|Maine)[^.;]{0,60}/i)||[])[0]||null;
  const packMatch=t.match(/\b(\d+(?:[,.]\d+)?)\s*l(?:it(?:ru|re|ri)?)?\b/i);
  const packLiters=packMatch?Number(packMatch[1].replace(",",".")):(/\b1000\s*ml\b/i.test(t)?1:null);
  const position=/(?:Pozitie de montare:|placute frana|uzura placute frana|Disc frana)\s*Fa[țt][aă]/i.test(t)?"FATA":/(?:Pozitie de montare:|placute frana|uzura placute frana|Disc frana)\s*Spate/i.test(t)?"SPATE":null;
  const side=/Pozitie de montare:[^\n]*(?:St[aâ]nga|stg)/i.test(t)?"STANGA":/Pozitie de montare:[^\n]*(?:Dreapta|dr\b)/i.test(t)?"DREAPTA":null;
  out.push({brand,product_code:pcode,description:desc,intercars_code:ic,supplier_price_ron:pp,retail_price_ron:rp,pack_liters:packLiters,position,side,stock,stock_R21:stock.R21||null,delivery:del,product_url:new URL(href,location.origin).href});
 }
 return out;
}
async function waitProducts(job,vehicle){
 const plan=productPlan(job);
 const st=await chrome.storage.local.get(["robotSearchIndex","robotItemIndex","robotProducts","robotMissingItems","robotHistoryOnly"]);
 const historyOnly=!!st.robotHistoryOnly;
 const itemIndex=st.robotItemIndex||0, searchIndex=st.robotSearchIndex||0;
 const item=plan[itemIndex];
 if(!item)throw new Error("Piesa solicitată nu a fost identificată în cerere.");
 const existing=st.robotProducts||[];
 const existingCodes=new Set(existing.map(product=>product.intercars_code).filter(Boolean));
 const matched=(historyOnly&&!item.exactCode&&!item.exactCodes?.length)?null:await waitFor(()=>{
    const group=brandGroup(item.label),vehicleMake=clean(job.vehicle_hints?.make||"");
   const products=extract().filter(p=>{
     const normalizeCode=x=>clean(x).replace(/[^a-z0-9]/gi,"").toUpperCase();
     const activeQueries=(historyOnly && item.exactCodes?.length)?item.exactCodes:item.queries;
     const usingHistoryFallback=!historyOnly && item.historyFallback && searchIndex===activeQueries.length-1;
     const exactCodes=(item.exactCodes||[item.exactCode]).filter(Boolean).map(normalizeCode);
     const exactHistoryCode=exactCodes.includes(normalizeCode(p.product_code));
     const sameCode=usingHistoryFallback || !exactCodes.length || exactCodes.includes(normalizeCode(p.product_code)) ||
       (searchIndex>0 && item.catalogName && normalizeCode(p.product_code)===normalizeCode(item.catalogName));
     if(sameCode && item.exactCode && !p.brand) p.brand=item.expectedBrand;
    return p.description && /(?:filtru|ulei|motor[oö]l|disc|pl[aă]cu[țt]|bu[sș]on|[sș]urub|curea|cap(?:[aă]t)?\s+(?:de\s+)?bar[aă]|etrier|bascul|bra[țt].*suspens|amortiz|rulment|burduf|protec[țt]ie|tampon|pivot|rotul|articula[țt]ie|bielet|tij[aă])/i.test(p.description) &&
     (!item.match || item.match.test(p.description)) &&
     sameCode &&
      (usingHistoryFallback || !item.expectedBrand || p.brand===item.expectedBrand ||
       (isOeBrand(item.expectedBrand) && isOeBrand(p.brand))) &&
     (!item.packLiters || !p.pack_liters || p.pack_liters===item.packLiters) &&
     (!item.reject || !item.reject.test(p.description)) &&
     (!item.position || !p.position || p.position===item.position) &&
     (!item.side || !p.side || p.side===item.side) &&
      (!group || exactHistoryCode || BRAND_RULES[group].includes(p.brand) ||
       (BRAND_RULES[group].includes("OE") && isOeBrand(p.brand))) &&
      oemMatchesVehicle(p.brand,vehicleMake) &&
      (!item.requiresOe || isOeBrand(p.brand)) &&
     (!item.requiresOneLiter || p.pack_liters===1);
   });
   if(item.preferOe && products.length){
     const inStock=p=>Object.values(p.stock||{}).some(value=>Number(String(value).replace(">",""))>0);
      const oeInStock=products.filter(p=>isOeBrand(p.brand) && oemMatchesVehicle(p.brand,vehicleMake) && inStock(p));
     if(oeInStock.length)return oeInStock;
      const alternatives=products.filter(p=>!isOeBrand(p.brand) && inStock(p));
     return alternatives.length?alternatives:null;
   }
   // Același cod găsit deja pentru altă punte nu dovedește că rezultatul
   // curent corespunde. Cerem cel puțin un produs nou pentru această poziție.
   const newProducts=products.filter(product=>!existingCodes.has(product.intercars_code));
   return newProducts.length?newProducts:null;
 },10000,300);
 if(!matched){
   const activeQueries=(historyOnly && item.exactCodes?.length)?item.exactCodes:item.queries;
   if(searchIndex+1<activeQueries.length){
     await setStage("SEARCH_PRODUCT",{robotSearchIndex:searchIndex+1});
     return searchProduct(job);
   }
   if(item.optional){
     const existing=st.robotProducts||[];
     await setStage("DONE",{robotProducts:existing});
     await sendJobMessage({action:"job_partial",payload:{vehicle:vehicle||{vin:job.vin,url:location.href},products:existing,message:"Filtrele au fost găsite. Uleiul OE de 1 litru nu a fost confirmat; prețul pe litru și cantitatea se completează manual înainte de ofertare."}});
     return;
   }
   const existing=st.robotProducts||[];
   const missing=[...new Set([...(st.robotMissingItems||[]),item.label])];
   if(itemIndex+1<plan.length){
    const saved=await sendJobMessage({action:"job_progress",payload:{
     vehicle:vehicle||{vin:job.vin,url:location.href},products:existing,
     message:`Nu am confirmat ${item.label}; continui cu următoarele poziții.`
    }});
    if(!saved?.ok)throw new Error("Progresul parțial nu a putut fi salvat: "+(saved?.error||"eroare server"));
    await setStage("SEARCH_PRODUCT",{robotItemIndex:itemIndex+1,robotSearchIndex:0,robotProducts:existing,robotMissingItems:missing});
    return searchProduct(job);
   }
   await setStage("DONE",{robotProducts:existing,robotMissingItems:missing});
   await sendJobMessage({action:"job_partial",payload:{
    vehicle:vehicle||{vin:job.vin,url:location.href},products:existing,
    message:`Ofertă parțială. Nu am confirmat: ${missing.join(", ")}.`
   }});
   return;
 }
 const seen=new Set(existing.map(p=>p.intercars_code));
 const products=[...existing,...matched.filter(p=>!seen.has(p.intercars_code)).map(p=>({
   ...p,
   requested_item:item.label,
   quantity:item.quantity||p.quantity||1,
   oe_code:item.exactCode||p.oe_code||null
 }))];
 const saved=await sendJobMessage({action:"job_progress",payload:{
   vehicle:vehicle||{vin:job.vin,url:location.href},
   products,
   message:`${item.label}: ${matched.length} variante găsite. Căutarea reviziei continuă.`
 }});
 if(!saved?.ok)throw new Error("Piesele găsite nu au putut fi salvate pentru verificare: "+(saved?.error||"eroare server"));
 if(itemIndex+1<plan.length){
   await setStage("SEARCH_PRODUCT",{robotItemIndex:itemIndex+1,robotSearchIndex:0,robotProducts:products});
   return searchProduct(job);
 }
 const missing=st.robotMissingItems||[];
 await setStage("DONE",{robotProducts:products,robotMissingItems:missing});
 await sendJobMessage({action:missing.length?"job_partial":"job_done",payload:{
  vehicle:vehicle||{vin:job.vin,url:location.href},products,
  message:missing.length
   ?`Ofertă parțială. Nu am confirmat: ${missing.join(", ")}.`
   :`Robot Inter Cars: ${plan.length} tipuri de piese, ${products.length} variante pregătite pentru verificare.`
 }});
}
let resuming=false;
async function resume(){
 if(resuming)return;
 resuming=true;
 try{
  const s=await state(); if(!s.currentJob)return;
  const j=s.currentJob;
  activeRunId=j.id;
  const planFingerprint=JSON.stringify(productPlan(j).map(item=>({label:item.label,codes:item.exactCodes||[],code:item.exactCode||null,position:item.position||null})));
  const runState=await chrome.storage.local.get(["robotRunId","robotPlanFingerprint"]);
  if(runState.robotRunId!==j.id||runState.robotPlanFingerprint!==planFingerprint){
   await chrome.storage.local.remove(["robotProducts","robotItemIndex","robotSearchIndex","robotMissingItems","robotVehicle","robotHistoryOnly"]);
   await chrome.storage.local.set({robotRunId:j.id,robotPlanFingerprint:planFingerprint,robotStage:"START",robotItemIndex:0,robotSearchIndex:0,robotProducts:[]});
   s.robotStage="START";
  }
  const stage=s.robotStage||"START";
  if(stage==="START")return await startVIN(j);
  if(stage==="WAIT_OE")return await waitOE();
  if(stage==="WAIT_VEHICLE")return await Promise.race([
    waitVehicle(j),
    sleep(45000).then(()=>{throw new Error(
      "Necesita alegere vehicul: Inter Cars nu a confirmat o varianta sigura in 45 de secunde."
    )})
  ]);
  if(stage==="SEARCH_PRODUCT")return await searchProduct(j);
  if(stage==="WAIT_PRODUCTS")return await waitProducts(j,s.robotVehicle);
 }catch(e){
  await sendJobMessage({action:"job_error",payload:{message:String(e.message||e)}});
 }finally{
  resuming=false;
 }
}
chrome.runtime.onMessage.addListener((m,s,r)=>{
 if(m.action==="resume"){
  r({ok:true,accepted:true});
  setTimeout(resume,0);
 }
});
setTimeout(resume,700);
})();
