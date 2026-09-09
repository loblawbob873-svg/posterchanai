'use strict';
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict'),crypto=require('crypto'),path=require('path');
const root=path.resolve(__dirname,'../..'),src=fs.readFileSync(root+'/static/js/client/app.js','utf8');
function part(start,end){const a=src.indexOf(start);assert(a>=0,start);const b=src.indexOf(end,a+start.length);assert(b>a,end);return src.slice(a,b);}
function setup(saved){
 const storage=saved||new Map(),ctx={console,crypto:crypto.webcrypto,TextEncoder,TextDecoder,Uint8Array,ArrayBuffer,Buffer,setTimeout,clearTimeout,URL,Map,Set,Date};ctx.window=ctx;ctx.localStorage={getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)};ctx.addEventListener=()=>{};ctx.document={querySelectorAll:()=>[]};vm.createContext(ctx);vm.runInContext(fs.readFileSync(root+'/static/vendor/nostr/nostr.bundle.js','utf8'),ctx);vm.runInContext(fs.readFileSync(root+'/static/js/client/store.js','utf8'),ctx);
 const save=ctx.Store.saveEvent;ctx.Store.saveEvent=ev=>{ctx.__incoming=JSON.stringify(ev);return save(vm.runInContext('JSON.parse(__incoming)',ctx))};const nt=ctx.NostrTools;const signed=(ev,sk)=>{ctx.__evjson=JSON.stringify(ev);return nt.finalizeEvent(vm.runInContext('JSON.parse(__evjson)',ctx),sk)};const secret=new Uint8Array(32).fill(1),other=new Uint8Array(32).fill(2),owner=nt.getPublicKey(secret);let seq=1;const event=(kind,tags=[],sk=secret)=>signed({kind,tags,content:'',created_at:1700000000+seq++},sk);
 Object.assign(ctx,{ME:{pubkey:owner},GUEST:false,signer:{},_FEDI_SOCIAL_KINDS:new Set([1,5,6,16]),_FEDI_DELIVERABLE_KINDS:new Set([1,5,6,16]),_refreshFediOnly:async()=>false,_fediOnly:()=>false,_fediOnlyEvent:e=>e.tags.some(t=>t[0]==='client-mode'&&t[1]==='fedi-only'),_enrichTags:(k,t)=>[...t,['client','PosterChan AI']],InstEmoji:{loaded:true},applySobLive(){},refreshOfflineBar(){},_guestPrompt(){},decorateCounts(){},toast(){},_isSob:()=>false,_tipNote:()=>null,zapAmount:()=>0,reactDisp:()=>'',renderNotificationsSoon(){},VIEW:'home',needProfile(){},needEvent(){},enc:String,_repostTag:()=>'<div class="repost-tag"></div>',noteCard:()=>'<article class="note">original</article>',eTags:(id,pk)=>[['e',id],['p',pk]]});
 let signs=0,sends=[];ctx.sign=async(k,c,t)=>{signs++;return signed({kind:k,content:c,tags:t,created_at:1700000000+seq++},secret)};ctx.Relay={publish:async ev=>{sends.push(ev);return {ok:true}}};
 vm.runInContext('let CIDX=null;function invalidateCounts(){CIDX=null;}'+part('  function buildCounts(){','  /* Amount label')+part('  async function publish(kind,','  // A guest tried')+part('  function _applyDeletion(ev){','  // Always-on deletion')+part('  const _repostVerificationMemo=','  async function doDelete(')+part('  function _noteHtml(ev){','  // Timeline renderer:'),ctx);
 return {ctx,storage,event,owner,other,signs:()=>signs,sends,button:()=>({disabled:false,setAttribute(){},removeAttribute(){}})};
}
async function run(){let count=0;
 // Mixed visibility/own kinds: original and other author's repost remain intact.
 {const h=setup(),c=h.ctx,original=h.event(1),a=h.event(6,[['e',original.id]]),b=h.event(16,[['e',original.id],['client-mode','fedi-only']]),foreign=h.event(6,[['e',original.id]],h.other);for(const e of [original,a,b,foreign])c.Store.saveEvent(e);c._refreshFediOnly=async()=>true;c._fediOnly=()=>true;await c.doRepost(original.id,original.pubkey,h.button());assert.equal(h.sends.length,2);assert.deepEqual(Array.from(h.sends[0].tags.filter(t=>t[0]==='e'),t=>t[1]),[a.id]);assert(!c._fediOnlyEvent(h.sends[0]));assert(c._fediOnlyEvent(h.sends[1]));assert(c.Store.has(original.id));assert(c.Store.has(foreign.id));assert(!c.Store.has(a.id));assert(!c.Store.has(b.id));count++;}
 // No optimistic disappearance; double clicks coalesce; same signed ID survives a failed ACK and reload.
 {const h=setup(),c=h.ctx,original=h.event(1),a=h.event(6,[['e',original.id]]);c.Store.saveEvent(original);c.Store.saveEvent(a);let resolve;c.Relay.publish=ev=>{h.sends.push(ev);return new Promise(r=>resolve=r)};const button=h.button(),pending=c.doRepost(original.id,original.pubkey,button);await new Promise(r=>setImmediate(r));assert(button.disabled);assert.equal(c.countsFor(original.id).iRt,true);assert.equal(c.Store.query([{kinds:[5]}]).length,0);await c.doRepost(original.id,original.pubkey,h.button());assert.equal(h.signs(),1);resolve({ok:false});await pending;assert(c.Store.has(a.id));const h2=setup(h.storage);for(const e of c.Store.all())h2.ctx.Store.saveEvent(e);await h2.ctx.doRepost(original.id,original.pubkey,h2.button());assert.equal(h2.signs(),0);assert.equal(h2.sends[0].id,h.sends[0].id);assert.equal(h2.sends[0].sig,h.sends[0].sig);assert(h2.ctx.Store.has(original.id));assert(!h2.ctx.Store.has(a.id));count++;}
 // Late history and a second device folding deletion before repost do not resurrect it.
 {const h=setup(),c=h.ctx,o=h.event(1),a=h.event(6,[['e',o.id]]);for(const e of[o,a])c.Store.saveEvent(e);await c.doRepost(o.id,o.pubkey,h.button());const d=h.sends[0],fresh=setup();fresh.ctx.Store.saveEvent(d);fresh.ctx.Store.saveEvent(a);fresh.ctx.Store.saveEvent(o);assert.equal(fresh.ctx.countsFor(o.id).iRt,false);assert.equal(fresh.ctx.countsFor(o.id).reposts,0);assert.equal(fresh.ctx._noteHtml(a),'');assert(fresh.ctx.Store.has(o.id));count++;}
 // A signed deletion by another author and a forged same-author tombstone cannot suppress a repost.
 {const h=setup(),c=h.ctx,o=h.event(1),a=h.event(6,[['e',o.id]]);for(const e of[o,a,h.event(5,[['e',a.id]],h.other)])c.Store.saveEvent(e);const fake={...h.event(5,[['e',a.id]]),sig:'0'.repeat(128)};c.Store.saveEvent(fake);assert.equal(c._repostDeleted(a),false);assert.equal(c.countsFor(o.id).iRt,true);count++;}
 // Switching accounts during signer wait stops publication and never deletes target/original.
 {const h=setup(),c=h.ctx,o=h.event(1),a=h.event(6,[['e',o.id]]);for(const e of[o,a])c.Store.saveEvent(e);let resolve;c.sign=()=>new Promise(r=>resolve=r);const pending=c.doRepost(o.id,o.pubkey,h.button());await new Promise(r=>setImmediate(r));c.ME={pubkey:c.NostrTools.getPublicKey(h.other)};resolve(h.event(5,[['e',a.id]]));await pending;assert.equal(h.sends.length,0);assert(c.Store.has(a.id));assert(c.Store.has(o.id));count++;}
 // Partial group success retries only remaining private request, preserving its signed event.
 {const h=setup(),c=h.ctx,o=h.event(1),a=h.event(6,[['e',o.id]]),b=h.event(6,[['e',o.id],['client-mode','fedi-only']]);for(const e of[o,a,b])c.Store.saveEvent(e);c.Relay.publish=async ev=>{h.sends.push(ev);return {ok:!c._fediOnlyEvent(ev)}};await c.doRepost(o.id,o.pubkey,h.button());assert(!c.Store.has(a.id));assert(c.Store.has(b.id));const failed=h.sends[1];c.Relay.publish=async ev=>{h.sends.push(ev);return {ok:true}};await c.doRepost(o.id,o.pubkey,h.button());assert.equal(h.signs(),2);assert.equal(h.sends[2].id,failed.id);assert(c.Store.has(o.id));count++;}
 // A late ACK cannot store/delete under the next account; durable receipt survives for its owner.
 {const h=setup(),c=h.ctx,o=h.event(1),a=h.event(6,[['e',o.id]]);for(const e of[o,a])c.Store.saveEvent(e);let resolve;c.Relay.publish=ev=>{h.sends.push(ev);return new Promise(r=>resolve=r)};const job=c.doRepost(o.id,o.pubkey,h.button());await new Promise(r=>setImmediate(r));assert(h.storage.size>0,'signed receipt persists before network wait');c.ME={pubkey:c.NostrTools.getPublicKey(h.other)};resolve({ok:true});await job;assert(c.Store.has(a.id));assert.equal(c.Store.query([{kinds:[5]}]).length,0);count++;}
 // Background deletion can arrive despite lost ACK. Retry is still the identical undo, never a new boost.
 {const h=setup(),c=h.ctx,o=h.event(1),a=h.event(6,[['e',o.id]]);for(const e of[o,a])c.Store.saveEvent(e);c.Relay.publish=async ev=>{h.sends.push(ev);return {ok:false}};await c.doRepost(o.id,o.pubkey,h.button());const deletion=h.sends[0];assert.equal(c._repostActionTitle(o.id,false),'retry undo repost');c.Store.saveEvent(deletion);c._applyDeletion(deletion);assert(!c.Store.has(a.id));const after=setup(h.storage);after.ctx.Store.saveEvent(o);await after.ctx.doRepost(o.id,o.pubkey,after.button());assert.equal(after.signs(),0);assert.equal(after.sends[0].id,deletion.id);assert.equal(after.sends[0].kind,5);assert(after.ctx.Store.has(o.id));count++;}
 // Persist failure refuses sending, and forged deletions cannot remove the original through hydration.
 {const h=setup(),c=h.ctx,o=h.event(1),a=h.event(6,[['e',o.id]]);for(const e of[o,a])c.Store.saveEvent(e);c.localStorage.setItem=()=>{throw Error('quota')};await c.doRepost(o.id,o.pubkey,h.button());assert.equal(h.sends.length,0);assert(c.Store.has(a.id));c._applyDeletion({...h.event(5,[['e',o.id]]),sig:'0'.repeat(128)});assert(c.Store.has(o.id));count++;}
 // Targeted deletion hydration includes cached public reposts, never private IDs, and rejects stale account responses.
 {const h=setup(),c=h.ctx,o=h.event(1),a=h.event(6,[['e',o.id]]),privateRepost=h.event(6,[['e',o.id],['client-mode','fedi-only']]);for(const e of[o,a,privateRepost])c.Store.saveEvent(e);let callback,queries=[];c.setTimeout=fn=>{callback=fn;return 1};c.$$=()=>[{dataset:{id:o.id}}];c.NO_IMAGES=false;vm.runInContext('let _ixT=null;'+part('  function hydrateCounts(){','  function decorateCounts(){'),c);c.Relay.query=async filters=>{queries.push(filters);return []};c.hydrateCounts();await callback();assert.equal(queries.length,2);assert.deepEqual(Array.from(queries[1][0]['#e']),[a.id]);let release;c.Relay.query=async filters=>filters[0].kinds[0]===5?new Promise(r=>release=r):[];c.hydrateCounts();const pending=callback();await new Promise(r=>setImmediate(r));c.ME={pubkey:c.NostrTools.getPublicKey(h.other)};release([h.event(5,[['e',a.id]])]);await pending;assert(c.Store.has(a.id));count++;}
 // An evicted original still on screen cannot be deleted without target ownership evidence.
 {const h=setup(),c=h.ctx,o=h.event(1),attack=h.event(5,[['e',o.id]],h.other);c.Store.saveEvent(o);c.Store.removeEvent(o.id);let removed=0;c.document.querySelectorAll=()=>[{remove(){removed++},closest(){return this}}];c._applyDeletion(attack);assert.equal(removed,0);count++;}
 // Cached checks avoid repeated signatures but mutations never inherit prior validity.
 {const h=setup(),c=h.ctx,o=h.event(1),a=h.event(6,[['e',o.id]]),d=h.event(5,[['e',a.id]]);for(const e of[o,a,d])c.Store.saveEvent(e);const original=c.NostrTools.verifyEvent;let checks=0;c.NostrTools={...c.NostrTools,verifyEvent:e=>{checks++;return original(e)}};for(let i=0;i<20;i++)assert(c._repostDeleted(a));assert.equal(checks,1);const stored=c.Store.get(d.id);stored.content='changed';assert.equal(c._repostDeleted(a),false);count++;}

 // A RECEIPT IS SPENT BY EVIDENCE. A lost ACK writes one; the deletion landing anyway (from a relay,
 // or another device) retires it. Left standing, the button says "retry undo repost" about finished
 // work AND that post can never be reposted again, because the receipt branch is checked first.
 {const h=setup(),c=h.ctx,o=h.event(1),a=h.event(6,[['e',o.id]]);for(const e of[o,a])c.Store.saveEvent(e);
  c.Relay.publish=async ev=>{h.sends.push(ev);return {ok:false}};
  await c.doRepost(o.id,o.pubkey,h.button());
  const deletion=h.sends[0],receiptKey='pc_repost_undo_'+h.owner+':'+o.id+':public';
  assert.equal(c._repostActionTitle(o.id,true),'retry undo repost','an unconfirmed undo must still offer its retry');
  assert(h.storage.has(receiptKey),'the signed deletion is kept for the retry');
  // The undo lands after all: the same signed request comes back over the wire.
  c.Store.saveEvent(deletion);c._applyDeletion(deletion);
  assert(!c.Store.has(a.id));assert(c.Store.has(o.id));
  assert.equal(c._repostActionTitle(o.id,false),'repost','a landed undo must stop advertising a retry');
  assert.equal(c._repostUndoPending(o.id),false);
  assert(!h.storage.has(receiptKey),'the spent receipt is dropped, not kept for ever');
  // ...and the button is a REPOST button again, not a second copy of a deletion nobody asked for.
  c.Relay.publish=async ev=>{h.sends.push(ev);return {ok:true}};
  await c.doRepost(o.id,o.pubkey,h.button());
  assert.equal(h.sends[1].kind,6,'the next click must repost, never re-send the spent deletion');
  count++;}
 // The same check must not fire early: a receipt whose target carries NO deletion is still the retry.
 {const h=setup(),c=h.ctx,o=h.event(1),a=h.event(6,[['e',o.id]]);for(const e of[o,a])c.Store.saveEvent(e);
  c.Relay.publish=async ev=>{h.sends.push(ev);return {ok:false}};
  await c.doRepost(o.id,o.pubkey,h.button());
  const foreign=h.event(5,[['e',a.id]],h.other);c.Store.saveEvent(foreign);   // somebody else's tombstone proves nothing
  assert.equal(c._repostUndoPending(o.id),true);
  assert(h.storage.has('pc_repost_undo_'+h.owner+':'+o.id+':public'));
  count++;}
 // YOUR OWN REPOST MUST NOT COMPETE WITH STRANGERS FOR A SLOT. The engagement fetch is capped, so on a
 // busy post the one event that decides repost-vs-undo can be missing — and the next click then
 // publishes a SECOND repost of a post this account already reposted, from another device.
 {const h=setup(),c=h.ctx,o=h.event(1),mine=h.event(6,[['e',o.id]]);c.Store.saveEvent(o);
  let callback,asked=[];c.setTimeout=fn=>{callback=fn;return 1};c.$$=()=>[{dataset:{id:o.id}}];c.NO_IMAGES=false;
  vm.runInContext('let _ixT=null;'+part('  function hydrateCounts(){','  function decorateCounts(){'),c);
  // A relay under load answers the crowded filter with nothing and the author-scoped one with the truth.
  c.Relay.query=async filters=>{asked.push(filters);
    if(filters[0].kinds[0]===5)return [];
    return filters.some(f=>Array.isArray(f.authors)&&f.authors.includes(h.owner))?[mine]:[];};
  c.hydrateCounts();await callback();
  assert(asked[0].some(f=>Array.isArray(f.authors)&&f.authors.includes(h.owner)&&f.kinds.includes(6)),
         'the count fetch must ask for THIS account\'s own reposts in its own filter');
  assert(c.Store.has(mine.id),'the own repost the crowded filter dropped still arrives');
  assert.equal(c.countsFor(o.id).iRt,true,'so the button offers the undo instead of a second repost');
  count++;}
 // The state has to be legible without a mouse: `title` is a hover tooltip and a phone has no hover.
 {const h=setup(),c=h.ctx,o=h.event(1),a=h.event(6,[['e',o.id]]);for(const e of[o,a])c.Store.saveEvent(e);
  c.Relay.publish=async ev=>{h.sends.push(ev);return {ok:false}};
  await c.doRepost(o.id,o.pubkey,h.button());
  const cls=new Set(),attrs={},btn={classList:{toggle:(n,on)=>{on?cls.add(n):cls.delete(n)}},setAttribute:(k,v)=>{attrs[k]=v},title:''};
  const note={dataset:{id:o.id},querySelector:sel=>sel.includes('"repost"')?btn:null};
  Object.assign(c,{$$:()=>[note],myReaction:()=>null,BOOKMARKS:new Set(),observeCelebrations(){},fmtSats:String});
  vm.runInContext(part('  function decorateCounts(){','  function timeAgo(ts)'),c);
  c.decorateCounts();
  assert.equal(attrs['aria-label'],'retry undo repost','the accessible name must carry the state, not just the tooltip');
  assert(cls.has('rt-unconfirmed'),'an unconfirmed undo is marked on the element, not only in a tooltip');
  count++;}
 console.log(JSON.stringify({passed:count}));
}
run().catch(e=>{console.error(e);process.exitCode=1});
