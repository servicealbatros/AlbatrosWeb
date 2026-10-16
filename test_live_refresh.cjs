const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const source=fs.readFileSync('static/live_refresh.js','utf8');
const flush=async()=>{for(let i=0;i<8;i++)await Promise.resolve()};
function harness(refresh){
 const timers=new Map(),listeners={},docListeners={},badge={style:{},dataset:{}};
 let id=0;
 const root={document:{hidden:false,getElementById:()=>badge,addEventListener:(k,v)=>docListeners[k]=v,removeEventListener:k=>delete docListeners[k]},navigator:{onLine:true},setTimeout:(fn,ms)=>{timers.set(++id,{fn,ms});return id},clearTimeout:i=>timers.delete(i),addEventListener:(k,v)=>listeners[k]=v,removeEventListener:k=>delete listeners[k]};
 vm.runInNewContext(source,{window:root,AbortController,Date});
 const control=root.AlbatrosLive.start({refresh});
 return {root,badge,timers,listeners,docListeners,control,run(ms){const entry=[...timers].find(([,v])=>v.ms===ms);assert.ok(entry,`Missing timer ${ms}`);timers.delete(entry[0]);entry[1].fn()}};
}
test('polls every 3 seconds and wakes immediately after returning to tab',async()=>{
 let calls=0;const h=harness(async()=>calls++);await flush();assert.equal(calls,1);assert.equal(h.badge.dataset.state,'connected');
 h.run(3000);await flush();assert.equal(calls,2);
 h.root.document.hidden=true;h.run(3000);await flush();assert.equal(calls,2);
 h.root.document.hidden=false;h.docListeners.visibilitychange();await flush();assert.equal(calls,3);h.control.stop();
});
test('does not overlap slow requests; a focus event queues a fresh read',async()=>{
 let finish,calls=0;const h=harness(()=>{calls++;return new Promise(r=>finish=r)});
 h.listeners.focus();h.listeners.online();assert.equal(calls,1);finish();await flush();h.run(0);assert.equal(calls,2);finish();await flush();h.control.stop();
});
test('connection failures recover automatically without page navigation',async()=>{
 let fail=true;const h=harness(async()=>{if(fail)throw Error('network')});await flush();assert.equal(h.badge.dataset.state,'reconnecting');
 fail=false;h.run(3000);await flush();assert.equal(h.badge.dataset.state,'connected');h.control.stop();
});
test('a hung fetch is aborted and retried',async()=>{
 let calls=0;const h=harness(signal=>{calls++;if(calls>1)return Promise.resolve();return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(Error('aborted'))))});
 h.run(12000);await flush();assert.equal(h.badge.dataset.state,'reconnecting');h.run(3000);await flush();assert.equal(calls,2);assert.equal(h.badge.dataset.state,'connected');h.control.stop();
});
test('offline badge does not claim data is live and reconnects when online',async()=>{
 let calls=0;const h=harness(async()=>calls++);await flush();h.root.navigator.onLine=false;h.listeners.offline();h.run(3000);await flush();assert.equal(calls,1);assert.equal(h.badge.dataset.state,'reconnecting');h.root.navigator.onLine=true;h.listeners.online();await flush();assert.equal(calls,2);h.control.stop();
});
function schedulerHarness(){
 const html=fs.readFileSync('static/scheduler.html','utf8');
 const code=html.slice(html.indexOf('let schedulerReadSequence='),html.indexOf('function status('));
 const stats={render:0,messages:0,requests:0};
 const context={data:{},weekStart:'2026-10-12',localDate:d=>d,document:{querySelector:()=>context.busy},busy:true,Date,api:async()=>({appointments:[{id:606,status:'neterminat'}]}),render:()=>stats.render++,renderRequests:()=>stats.requests++,renderNotifications:()=>stats.messages++,status:()=>{}};
 vm.createContext(context);vm.runInContext(code,context);return {context,stats};
}
test('messages refresh with an open modal while appointment form is left untouched',async()=>{
 const {context,stats}=schedulerHarness();await context.loadData({background:true});assert.equal(stats.messages,1);assert.equal(stats.render,0);
 context.busy=false;await context.loadData({background:true});assert.equal(stats.render,1);
 await context.loadData({background:true});assert.equal(stats.render,1);
});
test('late response from previous week cannot overwrite newly selected week',async()=>{
 const {context}=schedulerHarness();const reads=[];context.api=()=>new Promise(resolve=>reads.push(resolve));
 const first=context.loadData({background:true});context.weekStart='2026-10-19';const second=context.loadData({background:true});reads[1]({appointments:[{id:2}]});await second;reads[0]({appointments:[{id:1}]});await first;assert.equal(context.data.appointments[0].id,2);
});
