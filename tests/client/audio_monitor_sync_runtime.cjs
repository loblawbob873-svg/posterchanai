'use strict';
/* Two PosterChanOS monitors are two pages on ONE machine sharing ONE localStorage. Load the SHIPPED
 * osshell.js twice — one copy per "monitor", each with its own window — against one fake machine.
 * Reported: "Audio mixer is not in sync with other monitor, one says muted, the other says unmuted". */
const path=require('path');
const MOD=path.join(__dirname,'..','..','static','js','client','osshell.js');
const machine={muted:false,percent:40,statusReads:0};
const store=new Map(), pages=[];
function page(){
  const listeners=[];
  const win={
    listeners,
    addEventListener:(t,f)=>{ if(t==='storage')listeners.push(f); },
    removeEventListener:()=>{},
    localStorage:{
      getItem:k=>store.has(k)?store.get(k):null,
      setItem:(k,v)=>{ store.set(k,String(v)); for(const p of pages)if(p!==win)p.listeners.forEach(f=>f({key:k,newValue:String(v)})); },
      removeItem:k=>store.delete(k),
    },
    pcWM:{ windows:async()=>[], subscribe:async()=>true, onEvent:()=>()=>{} },
    /* FROZEN, like the object Electron's contextBridge exposes. A plain object here is how a Proxy
       over the real bridge passed this test and then threw on every read in the app. */
    pcAudio:Object.freeze({
      status:async()=>{ machine.statusReads++; return {output:{percent:machine.percent,muted:machine.muted}}; },
      setMuted:async(m)=>{ machine.muted=!!m; return {ok:true}; },
      setVolume:async(n)=>{ machine.percent=n; return {ok:true}; },
    }),
    pcNet:{ status:async()=>({online:true,kind:'wired'}) },
    pcPower:{ status:async()=>({battery:{present:false}}) },
    setInterval:()=>0, clearInterval:()=>{}, setTimeout, document:{},
  };
  pages.push(win);
  return win;
}
const vm=require('vm'),SRC=require('fs').readFileSync(MOD,'utf8');
function load(win){
  const ctx=vm.createContext(Object.assign(win,{Promise,JSON,Math,Date,String,Number,Array,Object,Map,Set,Proxy,Error,console}));
  vm.runInContext(SRC,ctx,{filename:'osshell.js'});
  return ctx.PCOSShell;
}
function ok(n,v){ if(!v){ console.error('FAIL '+n); process.exit(1);} console.log('  ok   '+n); }
(async()=>{
  const A=page(), a=load(A); const B=page(), b=load(B);
  await a.watch(()=>{});
  await b.watch(()=>{});
  // The tray's own read: with a frozen bridge this threw before anything else could happen.
  const s0=a.panelSummary(await a.panelState());
  ok('the tray reads the frozen audio bridge', s0.volume && s0.volume.known!==false && s0.volume.percent===40);
  const readsBefore=machine.statusReads;
  // Mute on monitor A, through the same bridge its mixer uses.
  if(typeof a.audio!=='function'){ console.error('FAIL the tray exposes no audio bridge'); process.exit(1); }
  await a.audio().setMuted(true,'sink');
  await new Promise(r=>setTimeout(r,50));
  ok('monitor B re-reads the machine when A changes the audio', machine.statusReads>readsBefore);
  const sB=b.panelSummary(await b.panelState());
  ok('monitor B shows muted', sB.volume && sB.volume.muted===true);
  console.log('OK audio sync');
})().catch(e=>{ console.error(e); process.exit(1); });
