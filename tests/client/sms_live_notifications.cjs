'use strict';
const vm=require('node:vm'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {webcrypto}=require('node:crypto');
const scenario=process.argv[2];
const state={events:[],toasts:[],queries:0,phoneReads:0,closes:0,timers:[],subs:[]};
let muted=false,phone=false,hold=null;
const storage=new Map();
const lockQueues=new Map();
const locks={request(name,fn){
 const next=(lockQueues.get(name)||Promise.resolve()).then(fn);
 lockQueues.set(name,next.catch(()=>{}));return next;
}};
const ctx={console,Date,Math,Map,Set,Promise,JSON,Uint8Array,TextEncoder,TextDecoder,URL,Blob,Buffer,crypto:webcrypto,
  setTimeout:()=>1,clearTimeout(){},setInterval:(fn,ms)=>{state.timers.push({fn,ms});return 1;},clearInterval(){},
  localStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,String(v)),removeItem:k=>storage.delete(k)},
  document:{hidden:false,visibilityState:'visible',hasFocus:()=>true,addEventListener(){},getElementById:()=>null},
  navigator:{locks},addEventListener(){},btoa:s=>Buffer.from(s,'binary').toString('base64'),atob:s=>Buffer.from(s,'base64').toString('binary')};
ctx.window=ctx;ctx.globalThis=ctx;
ctx.Store={query:()=>[],put(){}};
ctx.Relay={subscribe:(filters,handlers)=>{const sub={filters,...handlers};state.subs.push(sub);return sub;},close:()=>state.closes++,query:async()=>{state.queries++;return [];}};
ctx.__PC={VIEW:'home',ME:scenario==='late_login'?{}:{pubkey:'account-a'},enc:String,$:()=>null,
 notificationAllowed:type=>!muted,notifToast:(html,pic,onClick,type)=>{assert.equal(type,'sms');if(!muted)state.toasts.push({html,onClick});},
 osNotify:(title,body,opts)=>{assert.equal(opts.notificationType,'sms');if(!muted)state.events.push({title,body,opts});},
 capPlugin:()=>phone?{status:async()=>{state.phoneReads++;return {isDefault:true,telephony:true,canRead:true};}}:null,
 nip44dec:async(_,body)=>{if(hold)await hold;return body;},switchView:view=>{ctx.__PC.VIEW=view;},toast(){},
 filesIdx:()=>null,publish:async()=>({ok:true})};
vm.createContext(ctx);vm.runInContext(fs.readFileSync(process.env.PC_SMS_TEST_SOURCE||path.resolve(__dirname,'../../static/js/client/sms.js'),'utf8'),ctx);
const event=(id,patch={})=>({id,kind:30078,pubkey:ctx.__PC.ME.pubkey,created_at:Math.floor(Date.now()/1000),
 tags:[['d','pcai:sms:'+id],['l','pcai-sms']],content:JSON.stringify({address:'+15550123',body:'Hello',date:Date.now()+1,incoming:true,...patch})});
(async()=>{
 if(scenario==='late_login'){
  assert.equal(state.subs.length,0);ctx.__PC.ME={pubkey:'account-a'};
  await state.timers.find(t=>t.ms===3000).fn();
 }
 assert.equal(state.subs.length,1,'live SMS subscription must start before visiting Texts');
 assert.equal(state.queries,0,'startup notification subscription loaded archive history');
 assert.equal(state.phoneReads,0,'startup notification subscription read phone provider');
 const live=state.subs[0].onEvent;
 if(scenario==='multiwindow_reload'){
  // Separate module globals share only real platform boundaries: origin storage and Web Locks.
  const other={...ctx,__PC:{...ctx.__PC},Relay:{...ctx.Relay},document:{...ctx.document}};
  other.window=other;other.globalThis=other;delete other.PCSms;
  vm.createContext(other);vm.runInContext(fs.readFileSync(process.env.PC_SMS_TEST_SOURCE||path.resolve(__dirname,'../../static/js/client/sms.js'),'utf8'),other);
  const row=event('shared',{date:Date.now()+60000});
  await Promise.all([live(row),state.subs[1].onEvent(row)]);
  assert.equal(state.events.length,1,'independent windows emitted duplicate OS notifications');
  assert.equal(state.toasts.length,1,'independent windows emitted duplicate toast/sound');
  const reload={...ctx,__PC:{...ctx.__PC},Relay:{...ctx.Relay},document:{...ctx.document}};
  reload.window=reload;reload.globalThis=reload;delete reload.PCSms;
  vm.createContext(reload);vm.runInContext(fs.readFileSync(process.env.PC_SMS_TEST_SOURCE||path.resolve(__dirname,'../../static/js/client/sms.js'),'utf8'),reload);
  await state.subs[2].onEvent(row);
  assert.equal(state.events.length,1,'reload replayed an already announced message');
  reload.pcShell={backgroundOwner:false};await state.subs[2].onEvent(event('secondary',{date:Date.now()+60000}));
  assert.equal(state.events.length,1,'secondary native renderer emitted an alert');
 }else if(scenario==='duplicates'){
  let release;hold=new Promise(r=>release=r);const row=event('one');
  const first=live(row),second=live({...row,id:'relay-copy'});release();await Promise.all([first,second]);hold=null;
  await live(row);await live({...row,id:'metadata-update',created_at:row.created_at+1});assert.equal(state.events.length,1);assert.equal(state.toasts.length,1);
 }else if(scenario==='catchup_before_live'){
  const row=event('caught-up');await ctx.PCSms._absorb([row]);await live(row);
  assert.equal(state.events.length,1,'catch-up query swallowed the later live alert');await live(row);assert.equal(state.events.length,1);
 }else if(scenario==='history'){
  await live(event('old',{date:Date.now()-60000}));assert.equal(state.events.length,0);assert.equal(state.toasts.length,0);
 }else if(scenario==='outgoing'){
  await live(event('sent',{incoming:false}));assert.equal(state.events.length,0);
 }else if(scenario==='muted'){
  muted=true;await live(event('muted'));assert.equal(state.events.length,0);assert.equal(state.toasts.length,0);
 }else if(scenario==='phone'){
  phone=true;await live(event('native'));assert.equal(state.events.length,0);assert.equal(state.toasts.length,0);
 }else if(scenario==='active_thread'){
  ctx.__PC.VIEW='texts';ctx.PCSms._state().open=ctx.PCSms._key('+15550123');
  await live(event('reading'));assert.equal(state.events.length,0);assert.equal(state.toasts.length,0);
 }else if(scenario==='hidden_thread'){
  ctx.__PC.VIEW='texts';ctx.document.hidden=true;ctx.document.visibilityState='hidden';ctx.PCSms._state().open=ctx.PCSms._key('+15550123');
  await live(event('background'));assert.equal(state.events.length,1);assert.equal(state.toasts.length,1);
 }else if(scenario==='distinct_messages'){
  await Promise.all([live(event('first')),live(event('second',{address:'+15550456'}))]);
  assert.equal(state.events.length,2);assert.equal(new Set(state.events.map(e=>e.opts.tag)).size,2);
 }else if(scenario==='account_switch'){
  let release;hold=new Promise(r=>release=r);const pending=live(event('old-account'));
  ctx.__PC.ME={pubkey:'account-b'};release();await pending;hold=null;
  assert.equal(state.events.length,0);await state.timers.find(t=>t.ms===3000).fn();
  assert.equal(state.closes,1);assert.equal(state.subs.length,2);
  await live({...event('stale'),pubkey:'account-a'});assert.equal(state.events.length,0);
  await state.subs[1].onEvent(event('current'));assert.equal(state.events.length,1);
 }else{
  await live(event('new'));assert.equal(state.events.length,1);assert.equal(state.toasts.length,1);
  assert.equal(state.events[0].opts.route,'texts:%2B15550123');
  assert.equal(typeof state.toasts[0].onClick,'function');
 }
 console.log('PASS '+scenario);
})().catch(e=>{console.error(e);process.exit(1);});
