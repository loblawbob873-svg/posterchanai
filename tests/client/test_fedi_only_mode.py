"""Exercise the shipped composer, timeline filter and relay transports in a browser-like runtime."""
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[2]
APP=(ROOT/'static/js/client/app.js').read_text()
SOCIAL=APP[APP.index('  // A signed routing marker'):APP.index('  // A guest tried to do something')]
FILTER=APP[APP.index('  function isFediBridged(ev)'):APP.index('  function _drawTimeline(preserveScroll)')]


def test_routing_filters_retries_and_private_data():
    js=r'''
const vm=require('node:vm'), fs=require('node:fs'), assert=require('node:assert/strict');
const frames=[], sockets=[], requests=[], saved=new Map(), prefs=new Map(), messages=[];
let supported=true, mode=false, fail=false, noSettings=false, routeDown=false, seq=0, queued=0, history=[];
class WS{
 constructor(url){this.url=url;this.readyState=1;sockets.push(this);queueMicrotask(()=>this.onopen&&this.onopen());}
 send(raw){const m=JSON.parse(raw);frames.push(m);if(m[0]==='EVENT')queueMicrotask(()=>this.onmessage&&this.onmessage({data:JSON.stringify(['OK',m[1].id,true,''])}));}
 close(){this.readyState=3;}
}
const ctx={console,Map,Set,Promise,URL,JSON,Date,Number,String,Math,Array,Error,encodeURIComponent,queueMicrotask,setTimeout,clearTimeout,setInterval,clearInterval,
 WebSocket:WS, Worker:class{postMessage(){}}, document:{hidden:false,addEventListener(){}},navigator:{onLine:true},location:{origin:'https://app.test',protocol:'https:'},
 ME:{pubkey:'mine'},GUEST:false,signer:{},VIEW:'home', FOLLOWS:new Set(['native']),
 ClientSettings:{get:(k,d)=>prefs.has(k)?prefs.get(k):d,set:(k,v)=>prefs.set(k,v)},
 Store:{saveEvent:e=>saved.set(e.id,e),removeEvent:id=>saved.delete(id),query:()=>[]},
 Outbox:{canQueue:()=>true,add:()=>{queued++;return true;}},
 ensureAiSession:async()=>{},_standalone:()=>false,switchView:()=>{},
 fetch:async(url,opts)=>{
  requests.push(url);
  if(url.startsWith('/api/pleroma/private-events')){
   const u=new URL(url,'https://app.test'), before=u.searchParams.get('before'), id=u.searchParams.get('before_id');
   const rows=history.filter(e=>!before||e.created_at<Number(before)||(e.created_at===Number(before)&&e.id<id)).slice(0,200);
   return {ok:true,json:async()=>({events:rows})};
  }
  if(url==='/api/auth/settings')return {ok:!noSettings,json:async()=> supported ? {fedi_only:mode} : {}};
  assert.equal(url,'/api/pleroma/social-publish');if(routeDown)throw new Error('offline');const {event:ev,broadcast_only}=JSON.parse(opts.body);
  const privateEvent=ev.tags.some(t=>t[0]==='client-mode'&&t[1]==='fedi-only');
  return {ok:true,json:async()=> ev.kind===5&&!privateEvent&&!ev.content ? {route:'nostr'} : mode||privateEvent ? {route:'fediverse',ok:!fail&&!broadcast_only,msg:fail?'Fediverse unavailable':''}:{route:'nostr'}};
 },
 sign:async(kind,content,tags)=>({id:String(++seq),pubkey:'mine',kind,content,tags}),
 InstEmoji:{loaded:true},_enrichTags:(_k,t)=>t||[],invalidateCounts:()=>{},applySobLive:()=>{},toast:m=>messages.push(m),
 isReply:e=>e.reply===true,_applyDeletion:e=>{for(const t of e.tags)if(t[0]==='e')saved.delete(t[1]);}};
ctx.window=ctx;ctx.self=ctx;vm.createContext(ctx);
vm.runInContext(fs.readFileSync(RELAY,'utf8'),ctx);
vm.runInContext(SOCIAL+FILTER,ctx);
const R=ctx.Relay;
R.ready=async()=>true;
R._send=m=>{frames.push(m);queueMicrotask(()=>R._okWaiters.get(m[1].id).settle({ok:true}));return 1;};
(async()=>{
 const normal=await ctx.publish(1,'normal',[]);assert.equal(normal.ok,true);assert.equal(frames.length,1);
 mode=true;
 for(const kind of [1,6,7,16,5]){
  const before=frames.length;const r=await ctx.publish(kind,'private',[]);
  assert.equal(r.ok,true);assert.equal(r.fediverse,true);assert.equal(frames.length,before);
  assert.ok(r.ev.tags.some(t=>t[0]==='client-mode'&&t[1]==='fedi-only'));
 }
 const count=frames.length;
 const cleanup=await ctx.publish(5,'',[['e','old-stream'],['k','30311']],{publicDeletion:true});
 assert.equal(cleanup.ok,true);assert.equal(frames.length,count+1);
 assert.equal(cleanup.ev.tags.some(t=>t[0]==='client-mode'),false);
 const privatePost=await ctx.publish(1,'cached',[]), before=frames.length;
 mode=false;
 assert.equal(await R.publishTo(['wss://external.test'],privatePost.ev),0);
 assert.equal((await R.publishAuthed('wss://external.test',privatePost.ev,()=>{})).ok,false);
 assert.equal(R.publishFast(privatePost.ev),0);assert.equal(R.publishFastAll(privatePost.ev),0);
 assert.equal((await R.publish(privatePost.ev)).fediverse,true);
 assert.equal(frames.length,before);assert.equal(sockets.length,0);
 mode=true;fail=true;
 const failed=await ctx.publish(7,'+',[]);assert.equal(failed.ok,false);assert.equal(queued,0);
 assert.equal(saved.has(failed.ev.id),false);assert.equal(frames.length,before);
 fail=false;
 for(const kind of [30078,1059,22242]){assert.equal((await ctx.publish(kind,'private app data',[])).ok,true);}
 assert.equal(frames.length,before+3);
 noSettings=true;const failedRead=await ctx.publish(1,'must not leak',[]);assert.equal(failedRead.ok,false);
 assert.equal(frames.length,before+3);noSettings=false;
 const native={pubkey:'native',tags:[]}, bridge={pubkey:'puppet',tags:[['fedibridge','test']]};
 const interop={pubkey:'puppet2',tags:[['proxy','https://fedi.test/x','activitypub']]};
 ctx._setFediOnly(true);prefs.set('hideFediBridge',true);
 for(const view of ['home','global']){
  const f=ctx._tlFilter(view);assert.equal(f(native),false);assert.equal(f(bridge),true);assert.equal(f(interop),true);assert.equal(f(privatePost.ev),true);
 }
 ctx._setFediOnly(false);assert.equal(ctx._tlFilter('global')(native),true);assert.equal(ctx._tlFilter('global')(bridge),false);
 mode=false;noSettings=true;routeDown=true;
 const frameCount=frames.length, pending=await ctx.publish(1,'offline normal post',[]);
 assert.equal(pending.ok,false);assert.equal(pending.queued,true);assert.equal(queued,1);assert.equal(frames.length,frameCount);
 noSettings=false;routeDown=false;

 history=Array.from({length:205},(_,i)=>({id:'history-'+String(205-i).padStart(4,'0'),pubkey:'mine',kind:1,created_at:100,tags:[['client-mode','fedi-only']]}));
 assert.equal((await ctx._loadFediOnlyHistory(true)).length,205);
 const target=history[0].id;
 history=[{id:'deletion',pubkey:'mine',kind:5,created_at:200,tags:[['client-mode','fedi-only'],['e',target]]}];
 await ctx._loadFediOnlyHistory();assert.equal(saved.has(target),false);
 supported=false;mode=false;ctx._setFediOnly(false);
 const legacy=await ctx.publish(1,'older instance',[]);assert.equal(legacy.ok,true);assert.equal(legacy.fediverse,undefined);
 ctx.ME={pubkey:'other-account'};assert.equal(ctx._fediOnly(),false);
 process.stdout.write(JSON.stringify({ok:true,frames:frames.length}));
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
    for key,value in [('RELAY',str(ROOT/'static/js/client/relay.js')),('SOCIAL',SOCIAL),('FILTER',FILTER)]:
        js=js.replace(key,json.dumps(value))
    result=subprocess.run(['node'],input=js,text=True,capture_output=True,timeout=20)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['ok']
