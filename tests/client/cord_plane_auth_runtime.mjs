import fs from 'node:fs';import vm from 'node:vm';import assert from 'node:assert/strict';
Object.assign(globalThis,{window:globalThis,self:globalThis,document:{addEventListener(){}},location:{origin:'https://fixture.test',protocol:'https:'}});
Object.defineProperty(globalThis,'navigator',{value:{onLine:true}});
for(const [path,name] of [['../../static/vendor/nostr/nostr.bundle.js','NostrTools'],['../../static/js/client/cord-protocol.js','PosterCord'],['../../static/js/client/cord-reader.js','PosterCordReader']])vm.runInThisContext(fs.readFileSync(new URL(path,import.meta.url),'utf8')+`\nglobalThis.${name}=${name};`);
const secret=Uint8Array.from({length:32},(_,i)=>i+1),user=NostrTools.getPublicKey(secret),signEvent=async e=>NostrTools.finalizeEvent(e,secret),url='wss://plane.fixture';
const made=await PosterCord.createCommunity({name:'Plane AUTH fixture',owner:user,relays:[url],base:'https://fixture.test',signEvent});
const bundle={community_id:made.communityId,owner:user,owner_salt:made.secrets.ownerSalt,community_root:made.secrets.root,root_epoch:0,channels:[],relays:[url]},controls=made.events.slice(0,2),info=PosterCordReader.inspectControl(bundle,controls),channel=info.channels[0];
const note=await PosterCordReader.createChatWrap(bundle,controls,channel.id,'synthetic private fixture',user,signEvent);
let owner=user,rooms=[{communityId:made.communityId,cord:{bundle},channels:[channel]}];
class Socket{
 static all=[];constructor(url){this.url=url;this.readyState=0;this.sent=[];Socket.all.push(this);queueMicrotask(()=>{if(this.readyState===0){this.readyState=1;this.onopen?.();}});}
 send(wire){assert.equal(this.readyState,1);this.sent.push(JSON.parse(wire));}
 receive(packet){return this.onmessage?.({data:JSON.stringify(packet)});}
 close(){if(this.readyState===3)return;this.readyState=3;this.onclose?.();}
}
globalThis.WebSocket=Socket;globalThis.Worker=class{postMessage(m){queueMicrotask(()=>this.onmessage({data:{id:m.id,ok:true,data:m.args.events.map(e=>({id:e.id,valid:NostrTools.verifyEvent(e)}))}}));}};
vm.runInThisContext(fs.readFileSync(process.env.PC_PLANE_RELAY_SOURCE||new URL('../../static/js/client/relay.js',import.meta.url),'utf8'));
let accountSigns=0;Relay.setAuthSigner(async e=>{accountSigns++;return signEvent(e)},()=>owner);
const tick=()=>new Promise(r=>setImmediate(r));
const src=fs.readFileSync(new URL('../../static/js/client/concord.js',import.meta.url),'utf8');
const ctx={window:globalThis,roomControls:new Map([[made.communityId,controls]]),saved:()=>rooms,roomIdentity:r=>r.communityId};vm.createContext(ctx);
vm.runInContext(src.slice(src.indexOf('  function cordControlStamp('),src.indexOf('  /* Tuple encoding')),ctx);
const p={viewer:()=>({pubkey:owner}),relayQuery:()=>{throw Error('plane must not use shared account socket')},relayQueryFrom:(u,f,o)=>Relay.queryFrom(u,f,o)};
const plane=()=>ctx.cordPlaneContext(p,bundle,controls,rooms[0]);
// Real synthetic server boundary: author-gated REQ/EVENT requires a valid signature by that plane.
async function authenticate(socket,author,isPublish=false){
 await tick();const first=socket.sent.find(x=>x[0]===(isPublish?'EVENT':'REQ'));assert(first,'wire request sent');
 const challenge='exact:'+socket.url+':'+Socket.all.indexOf(socket);
 socket.receive(['AUTH',challenge]);socket.receive(isPublish?['OK',first[1].id,false,'ERROR: auth-required: author must authenticate']:['CLOSED',first[1],'ERROR: auth-required: requested filter requires authentication']);await tick();
 const auth=socket.sent.find(x=>x[0]==='AUTH')?.[1];assert(auth,'author-gated relay must receive AUTH');
 assert.equal(auth.pubkey,author,'AUTH must match queried plane, not account');assert(NostrTools.verifyEvent(auth));
 assert.deepEqual(auth.tags,[['relay',socket.url],['challenge',challenge]]);assert.equal(socket.sent.filter(x=>x[0]===first[0]).length,1,'no replay before positive AUTH acknowledgement');
 socket.receive(['OK',auth.id,true]);await tick();assert.deepEqual(socket.sent.filter(x=>x[0]===first[0]),[first,first],'replay exact original bytes');return first;
}
// Already-pooled URL still gets a separate plane connection, leaving shared account AUTH untouched.
Relay._conns.set(url,{ws:{readyState:1}});
for(const author of [info.controlPubkeys[0],channel.streamPubkeys[0]]){
 const pending=ctx.cordQuery(p,[url],[{kinds:[1059],authors:[author],limit:10}],{timeout:1000,plane:plane()});await tick();const socket=Socket.all.at(-1);const req=await authenticate(socket,author);
 const event=author===note.wrap.pubkey?note.wrap:controls.find(ev=>ev.pubkey===author);
 socket.receive(['EVENT',req[1],event]);socket.receive(['EOSE',req[1]]);assert.equal(JSON.stringify((await pending).map(e=>e.id)),JSON.stringify([event.id]));
}
const stable=plane();ctx.roomControls.set(made.communityId,[...controls].reverse());assert(stable.current(),'equivalent reordered control history does not retire AUTH');ctx.roomControls.set(made.communityId,controls);
// A multi-plane filter is split into isolated single-author requests, not partially authorized.
const allAuthors=[info.controlPubkeys[0],channel.streamPubkeys[0]];
const combined=ctx.cordQuery(p,[url],[{kinds:[1059],authors:allAuthors}],{timeout:1000,plane:plane()});
for(const author of allAuthors){await tick();const socket=Socket.all.at(-1),req=await authenticate(socket,author);assert.deepEqual(req[2].authors,[author]);const event=author===note.wrap.pubkey?note.wrap:controls.find(ev=>ev.pubkey===author);socket.receive(['EVENT',req[1],event]);socket.receive(['EOSE',req[1]]);}
assert.equal((await combined).length,2);
// Disjoint epoch dates must not move the shared pagination cursor past unread recent history.
vm.runInContext(src.slice(src.indexOf('  function envelopeCacheKey('),src.indexOf('  /* `thread`')),ctx);
const history=[...Array.from({length:1500},(_,i)=>({id:'recent-'+i,pubkey:allAuthors[0],created_at:10000+i})),...Array.from({length:1000},(_,i)=>({id:'old-'+i,pubkey:allAuthors[1],created_at:1+i}))];
const paged={...p,relayQueryFrom:async(_u,[filter])=>history.filter(ev=>filter.authors.includes(ev.pubkey)&&(filter.until==null||ev.created_at<=filter.until)).sort((a,b)=>b.created_at-a.created_at).slice(0,filter.limit)};
const complete=await ctx.queryEnvelopeHistory(paged,[url],allAuthors,[],{plane:plane()});assert.equal(complete.length,2500,'per-plane pagination preserves the recent epoch intermediate tail');assert(complete.some(e=>e.id==='recent-0'));
assert.equal(accountSigns,0,'plane reads never ask the account signer');
// Live stream reconnect reauthenticates the same plane on a new exact challenge/socket.
const received=[],scope=ctx.cordPlaneAuth(p,plane(),note.wrap.pubkey,[url]);
const stop=Relay.subscribeFrom([url],[{kinds:[1059],authors:[note.wrap.pubkey]}],{authScope:scope,live:true,timeout:0,onEvent:e=>received.push(e.id)});await tick();let socket=Socket.all.at(-1),req=await authenticate(socket,note.wrap.pubkey);socket.receive(['EVENT',req[1],note.wrap]);await tick();assert.deepEqual(received,[note.wrap.id]);
socket.close();await new Promise(r=>setTimeout(r,1050));const replacement=Socket.all.at(-1);assert.notEqual(socket,replacement);await authenticate(replacement,note.wrap.pubkey);stop();
// Room publish uses exactly the existing signed wrap on its own plane socket.
for(const event of [controls[0],note.wrap]){const pubScope=ctx.cordPlaneAuth(p,plane(),event.pubkey,[url]),pending=Relay.publishTo([url],event,{includeManaged:true,detailed:true,timeout:1000,authScope:pubScope});await tick();socket=Socket.all.at(-1);await authenticate(socket,event.pubkey,true);socket.receive(['OK',event.id,true]);assert.equal((await pending).accepted,1);}
// Actual Webxdc peer publisher/history and realtime subscriber use the scoped transport too.
p.signTemplate=signEvent;p.relayPublishRoom=(urls,event,authScope)=>Relay.publishTo(urls,event,{includeManaged:true,detailed:true,timeout:1000,authScope});
ctx.PC=()=>p;ctx.webxdcCordParts=async()=>({p,reader:PosterCordReader,bundle,controls,channel,room:rooms[0],relays:[url],streamPubkeys:channel.streamPubkeys,plane:plane()});
vm.runInContext(src.slice(src.indexOf('  async function webxdcSubscribe('),src.indexOf('  window.PCConcord=')),ctx);
const webCtx={protocol:'concord2'},signal=JSON.stringify({op:'ad',topic:'fixture',addr:'fixture-node'});
const peerPending=ctx.webxdcPeerPublish(webCtx,signal);await tick();socket=Socket.all.at(-1);await authenticate(socket,note.wrap.pubkey,true);const peerWrap=socket.sent.find(m=>m[0]==='EVENT')[1];socket.receive(['OK',peerWrap.id,true]);const peerMade=await peerPending;assert.equal(peerMade.wrap.id,peerWrap.id);
const peerHistory=ctx.webxdcPeerQuery(webCtx);await tick();socket=Socket.all.at(-1);req=await authenticate(socket,note.wrap.pubkey);socket.receive(['EVENT',req[1],peerWrap]);socket.receive(['EOSE',req[1]]);const peerRows=await peerHistory;assert.equal(peerRows.length,1);assert.equal(peerRows[0].content,signal);
const updates=[],update=await PosterCordReader.createWebxdcWrap(bundle,controls,channel.id,'fixture-update',user,signEvent,[['i','fixture-session'],['rt','1']],true);
const livePending=ctx.webxdcSubscribe(webCtx,'fixture-session',true,row=>updates.push(row));await tick();socket=Socket.all.at(-1);req=await authenticate(socket,update.wrap.pubkey);const webStop=await livePending;socket.receive(['EVENT',req[1],update.wrap]);await tick();assert.equal(updates.length,1);assert.equal(updates[0].content,'fixture-update');assert.equal(webStop.publish(update.wrap),1);webStop();
assert.equal(accountSigns,0,'Webxdc room traffic does not authenticate the account pool');
// Revoked membership/rekey and account switches suppress held signer completions and replay.
for(const change of ['account','membership','rekey']){
 owner=user;rooms=[{communityId:made.communityId,cord:{bundle},channels:[channel]}];ctx.roomControls.set(made.communityId,controls);
 let release;const held=ctx.cordPlaneAuth(p,plane(),note.wrap.pubkey,[url]),delayed={...held,sign:async tpl=>{await new Promise(r=>release=r);return held.sign(tpl)}};
 const job=Relay.queryFrom([url],[{kinds:[1059],authors:[note.wrap.pubkey]}],{exact:true,timeout:1000,authScope:delayed});await tick();socket=Socket.all.at(-1);const id=socket.sent[0][1];socket.receive(['AUTH','held']);socket.receive(['CLOSED',id,'auth-required: plane']);await tick();
 if(change==='account')owner='f'.repeat(64);else if(change==='membership')rooms=[];else ctx.roomControls.set(made.communityId,[...controls,{id:'new-control-generation'}]);
 release();assert.deepEqual(await job,[]);assert(!socket.sent.some(x=>x[0]==='AUTH'));
 Relay._queryFromCooldown.clear();
}
// A plane AUTH timeout is a transient transport failure; explicit denial stays terminal.
owner=user;rooms=[{communityId:made.communityId,cord:{bundle},channels:[channel]}];ctx.roomControls.set(made.communityId,controls);
const timeoutScope=ctx.cordPlaneAuth(p,plane(),note.wrap.pubkey,[url]);
const realTimer=globalThis.setTimeout,authTimers=[];
globalThis.setTimeout=(fn,ms,...args)=>ms===12000?(authTimers.push(()=>fn(...args)),{fixtureTimer:true}):realTimer(fn,ms,...args);
const recover=Relay.subscribeFrom([url],[{kinds:[1059],authors:[note.wrap.pubkey]}],{authScope:timeoutScope,live:true,timeout:0});
await tick();socket=Socket.all.at(-1);socket.receive(['AUTH','timeout']);socket.receive(['CLOSED',socket.sent[0][1],'auth-required: plane']);await tick();
assert(socket.sent.some(m=>m[0]==='AUTH'));const timedOut=socket;authTimers.shift()();globalThis.setTimeout=realTimer;
await new Promise(r=>setTimeout(r,1050));socket=Socket.all.at(-1);assert.notEqual(socket,timedOut,'transient plane AUTH timeout must reconnect without navigating');await authenticate(socket,note.wrap.pubkey);
assert.equal(recover.publish({...note.wrap,kind:1}),0,'live capability cannot publish another event kind');
assert.equal(recover.publish(note.wrap),1,'live publication uses the current replacement socket');recover();
const denied=Relay.subscribeFrom([url],[{kinds:[1059],authors:[note.wrap.pubkey]}],{authScope:timeoutScope,live:true,timeout:0});await tick();socket=Socket.all.at(-1);socket.receive(['AUTH','denied']);socket.receive(['CLOSED',socket.sent[0][1],'auth-required: plane']);await tick();socket.receive(['OK',socket.sent.find(m=>m[0]==='AUTH')[1].id,false]);const deniedCount=Socket.all.length;await new Promise(r=>setTimeout(r,1050));assert.equal(Socket.all.length,deniedCount,'explicit plane denial cannot create an AUTH storm');denied();
assert.throws(()=>Relay.subscribeFrom([url],[],{authScope:timeoutScope}),/invalid plane/);
assert.throws(()=>Relay.subscribeFrom([url],[{authors:[note.wrap.pubkey],kinds:[]}],{authScope:timeoutScope}),/invalid plane/);
// Imported archived epochs cannot multiply live fanout without a fixed budget.
const realReader=window.PosterCordReader;window.PosterCordReader={...realReader,createPlaneAuth:(_b,_c,pubkey)=>({pubkey,sign(){}})};
let liveSockets=0;const budgetRelay={subscribeFrom:(urls,filters,options)=>{liveSockets+=Math.min(urls.length,options.max);const stop=()=>{};stop.ready=Promise.resolve(true);return stop;}};
const budget=ctx.cordPlaneSubscribe(p,budgetRelay,Array.from({length:8},(_,i)=>`wss://budget${i}.fixture`),[{kinds:[1059],authors:Array.from({length:1000},(_,i)=>String(i).padStart(64,'0'))}],{live:true,timeout:0},plane());
assert.equal(liveSockets,8,'live sockets are capped even with 1000 archived roots');budget();window.PosterCordReader=realReader;
// Account switch before socket-open cannot transmit even the first signed EVENT.
owner=user;rooms=[{communityId:made.communityId,cord:{bundle},channels:[channel]}];ctx.roomControls.set(made.communityId,controls);
const openingScope=ctx.cordPlaneAuth(p,plane(),note.wrap.pubkey,[url]);
const opening=Relay.publishTo([url],note.wrap,{includeManaged:true,detailed:true,timeout:1000,authScope:openingScope});const unopened=Socket.all.at(-1);owner='e'.repeat(64);assert.equal((await opening).uncertain,false);assert.equal(unopened.sent.length,0);
// Capability cannot be converted into a note signer, used for unknown plane or another relay.
const cap=PosterCordReader.createPlaneAuth(bundle,controls,note.wrap.pubkey,[url]);assert.deepEqual(Object.keys(cap).sort(),['pubkey','sign']);
assert.throws(()=>PosterCordReader.createPlaneAuth(bundle,controls,user,[url]),/not held/);
assert.throws(()=>cap.sign({kind:1,content:'no',created_at:Math.floor(Date.now()/1000),tags:[]}));
assert.throws(()=>cap.sign({kind:22242,content:'',created_at:Math.floor(Date.now()/1000),tags:[['relay','wss://other.fixture'],['challenge','x']]}));
// Drive the actual moderation handler across confirmation and aggregate ACK account races.
ctx.state={community:0};ctx.render=()=>{};ctx.URL=URL;
vm.runInContext(src.slice(src.indexOf('  function normalizeRelay('),src.indexOf('\n',src.indexOf('  function normalizeRelay(')))+src.slice(src.indexOf('  function roomRelays('),src.indexOf('  function cordControlStamp(')),ctx);let writes=0,signatures=0,publishes=0,releaseModeration;
ctx.save=()=>{writes++};
const moderationLine=src.slice(src.indexOf('    const banMember=async'),src.indexOf('\n',src.indexOf('    const banMember=async')));
vm.runInContext(moderationLine+';globalThis.moderate=banMember;',ctx);
for(const phase of ['confirmation','aggregate-ack']){
 owner=user;rooms=[{communityId:made.communityId,cord:{bundle},channels:[channel]}];ctx.roomControls.set(made.communityId,controls);writes=signatures=publishes=0;releaseModeration=null;
 ctx.p={viewer:()=>({pubkey:owner}),toast(){},uiConfirm:()=>phase==='confirmation'?new Promise(r=>releaseModeration=r):true,
  signTemplate:async e=>{signatures++;return signEvent(e)},relayPublishRoom:async()=>{publishes++;await new Promise(r=>releaseModeration=r);return{ok:true,accepted:1}}};
 const moderation=ctx.moderate('b'.repeat(64));for(let i=0;i<30&&!releaseModeration;i++)await tick();assert(releaseModeration,phase);
 owner='e'.repeat(64);releaseModeration(true);await moderation;assert.equal(writes,0,'late moderation cannot mutate new account store');
 if(phase==='confirmation'){assert.equal(signatures,0);assert.equal(publishes,0);}else assert.equal(publishes,1);
}
console.log('real signed control/chat history/live/reconnect/publish plane AUTH and owner/rekey guards PASS');process.exit(0);
