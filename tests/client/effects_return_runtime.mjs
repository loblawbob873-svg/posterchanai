import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const src=fs.readFileSync(process.argv[2],'utf8');
const helpers=src.slice(src.indexOf('  function _effectReturnValid('),src.indexOf('  async function launchEffectStudio('));
const send=src.slice(src.indexOf('  async function sendEffectReply('),src.indexOf('  // Combined-effects rules'));
const artifact=src.slice(src.indexOf('  async function replyFileUrl('),src.indexOf('  // Share generated media as a NEW Nostr post:'));
const launch=src.slice(src.indexOf('  async function launchEffectStudio('),src.indexOf('  // 🎞️ Meme Builder:',src.indexOf('  async function launchEffectStudio(')));
function setup(){
 const actions=[],back={hidden:true},btn={disabled:false,textContent:'Send the Reply',classList:{add(){}}};
 const to={id:'source-post',pk:'source-author'};
 const target={owner:'alice',view:'global',scroll:{pcv:'global',top:480,anchor:{key:'reading-post',dy:-20}},ready:true,conversation:12};
 const c={ME:{pubkey:'alice'},VIEW:'ai',_routing:false,_ai:{fxReturn:target,convId:12,replyTo:to,fxMedia:{result:{url:'https://media/effect.png'}}},
  $:()=>back,toast:t=>actions.push(['toast',t]),switchView:v=>{c.VIEW=v;actions.push(['view',v]);},
  _restoreNavScroll:s=>actions.push(['scroll',s.top,s.anchor?.key]),
  publish:async(kind,url,tags)=>{actions.push(['publish',kind,url,tags]);return {ok:true}},
  eTags:(id,pk)=>[id,pk],_btnText:(b,t)=>{b.textContent=t},_fileToPublicUrl:async()=> 'https://media/artifact.png',
  NT:()=>({nip19:{decode:()=>({type:'nevent',data:{id:'original-thread',relays:['wss://relay']}})}}),
  openThread:(id)=>{c.VIEW='thread';actions.push(['thread',id])},renderProfileView:pk=>actions.push(['profile',pk]),
  ensureAiSession:async()=>({can_ai:true}),_navState:v=>({pcv:v,top:480}),_entityFromPath:()=>null,
  modal(){},window:{},location:{href:'/client'},history:{replaceState:(st,title,url)=>actions.push(['history',url])}};
 vm.createContext(c);vm.runInContext(helpers+send+artifact+launch,c);
 return {c,actions,back,btn,target};
}
for(const method of ['sendEffectReply','replyFileUrl']){
 let f=setup();await f.c[method](method==='sendEffectReply'?'result':'artifact',f.btn);
 assert.equal(f.actions.filter(x=>x[0]==='publish').length,1);
 assert.deepEqual(f.actions.find(x=>x[0]==='publish').slice(-1)[0],['source-post','source-author']);
 assert.deepEqual(f.actions.filter(x=>['view','scroll'].includes(x[0])),[['view','global'],['scroll',480,'reading-post']]);
 assert.equal(f.c._ai.replyTo,null);assert.equal(f.back.hidden,true);
 f=setup();f.c.publish=async()=>({ok:false});await f.c[method](method==='sendEffectReply'?'result':'artifact',f.btn);
 assert.equal(f.c.VIEW,'ai');assert.equal(f.c._ai.fxReturn,f.target);assert.equal(f.btn.disabled,false);
 f.c._syncEffectReturn();assert.equal(f.back.hidden,false,'Back remains available after failed publication');
 f=setup();f.c.ME={pubkey:'bob'};await f.c[method](method==='sendEffectReply'?'result':'artifact',f.btn);
 assert(!f.actions.some(x=>x[0]==='publish'||x[0]==='view'));
 f=setup();f.c.publish=async()=>{f.c._ai.convId=13;return {ok:true}};
 await f.c[method](method==='sendEffectReply'?'result':'artifact',f.btn);
 assert.equal(f.c.VIEW,'ai','late publish must not pull a new conversation away');
}
let f=setup();f.target.windowReturn=()=>{f.actions.push(['focus-original']);return true};
f.c._returnFromEffect(f.target);assert.deepEqual(f.actions,[['focus-original']],'desktop return must not repaint route');
f=setup();f.target.windowReturn=()=>false;assert.equal(f.c._returnFromEffect(f.target),false);assert.equal(f.c.VIEW,'ai');
f=setup();f.target.view='thread';f.target.scroll.pcv='thread';f.target.entity={q:'nostr-entity'};f.target.url='/original-nevent';
f.c._returnFromEffect(f.target);assert.deepEqual(f.actions[0],['thread','original-thread']);assert.deepEqual(f.actions[1],['history','/original-nevent']);
f=setup();f.c._fileToPublicUrl=async()=>{f.c.ME={pubkey:'bob'};return 'url'};
await f.c.replyFileUrl('artifact',f.btn);assert(!f.actions.some(x=>x[0]==='publish'));
f=setup();f.c.VIEW='global';f.c.window.PCOS={captureReturnTarget:()=>()=>true};f.c.PCOS=f.c.window.PCOS;
await f.c.launchEffectStudio('https://image',{id:'post',pk:'author'});
assert.equal(f.c._ai.fxReturn.owner,'alice');assert.equal(f.c._ai.fxReturn.view,'global');assert.equal(f.c._ai.fxReturn.scroll.top,480);
assert.equal(f.c._ai.pendingFx.url,'https://image');
f=setup();f.c.VIEW='global';f.c.ensureAiSession=async()=>{f.c.VIEW='messages';return {can_ai:true}};
await f.c.launchEffectStudio('https://image',{id:'post',pk:'author'});assert.equal(f.c.VIEW,'messages');
f=setup();f.c.VIEW='profile';f.c.ensureAiSession=async()=>{f.c.location.href='/different-profile';return {can_ai:true}};
await f.c.launchEffectStudio('https://image',{id:'post',pk:'author'});assert.equal(f.c.VIEW,'profile');assert(!f.actions.some(x=>x[0]==='view'));
console.log('PASS Effects source return, both publish paths, failures, account/conversation races and native thread restoration');
