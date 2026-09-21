/* CONCORD MUST NOT SIGN THE SAME THING TWICE.
 *
 * "Users are getting censored on Amethyst because posterchan is sending multiple similar events for
 * concord." Amethyst's AntiSpamFilter hashes content+tags (NOT the author) of every note/addressable
 * it sees; a second event with the same hash and a different id marks the author, and five hide
 * them. So re-signing an unchanged thing is not harmless noise, it is how an account gets hidden.
 *
 * Each block drives SHIPPED concord.js code and fails on the pre-fix tree:
 *   1. the 33302 membership vault is re-signed only when the membership CHANGED;
 *   2. "Publish listing" re-sends the SAME signed kind-1 announcement, never a second one;
 *   3. a Concord poll vote that repeats your current answer publishes nothing;
 *   4. a Webxdc update is published to the ROOM's relays only, never the user's public pool.
 */
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {makeRealm,loadInto,into} from '../../botframework/cord_realm.mjs';
const root=new URL('../../',import.meta.url),source=fs.readFileSync(new URL('static/js/client/concord.js',root),'utf8');
const slice=(from,to)=>{const a=source.indexOf(from),b=source.indexOf(to,a+1);assert(a>=0&&b>a,'missing '+from);return source.slice(a,b);};

// ---------------------------------------------------------------- 1. membership vault
{
  const ctx=makeRealm(),copy=into(ctx);loadInto(ctx,new URL('static/vendor/nostr/nostr.bundle.js',root));
  const NT=ctx.NostrTools,sk=NT.generateSecretKey(),owner=NT.getPublicKey(sk),key=NT.nip44.v2.utils.getConversationKey(sk,owner);
  const hex=n=>n.toString(16).padStart(64,'0'),b64=h=>Buffer.from(h,'hex').toString('base64url');
  let relay=[],published=[];
  const sign=template=>NT.finalizeEvent(copy(template),sk);
  ctx.membershipEvents=async()=>copy(relay);ctx.CORD_RELAYS=copy(['wss://test.example']);
  ctx.roomIdentity=room=>room.communityId;ctx.forgetLeftCommunity=()=>{};ctx.rememberLeftCommunity=()=>{};
  const api={viewer:()=>({pubkey:owner}),nip44enc:async(_p,t)=>NT.nip44.v2.encrypt(t,key),nip44dec:async(_p,t)=>NT.nip44.v2.decrypt(t,key),
    signTemplate:async t=>sign(t),relayPublishTo:async(_u,ev)=>{published.push(ev);const d=ev.tags[0][1];relay=relay.filter(e=>e.tags[0][1]!==d);relay.push(ev);return true;}};
  vm.runInContext(slice('  function cordListHex(','  function cordListMaterial(')+slice('  // CORD-02 §8: canonical','  // Only encrypted, signed envelopes cross'),ctx);
  const material=(n,extra={})=>({owner:b64(owner),owner_salt:b64(hex(2)),community_root:b64(hex(1000+n)),root_epoch:0,channels:[],name:'room '+n,relays:['wss://test.example'],...extra});
  const room=(n,extra={})=>copy({communityId:hex(n),url:'https://example.test/invite/'+n+'#token',name:extra.name||'room '+n,cord:{bundle:{community_id:hex(n),...material(n,extra)}}});

  await ctx.persistArmadaMembership(api,room(1));
  assert.equal(published.length,1,'a first join writes the vault');
  for(let i=0;i<5;i++)await ctx.persistArmadaMembership(api,room(1));
  assert.equal(published.length,1,'re-persisting an unchanged membership must not re-sign the vault (was one event per call)');
  await ctx.persistArmadaMemberships(api,[room(1)]);
  assert.equal(published.length,1,'the backfill path is the same rule');
  await ctx.persistArmadaMembership(api,room(1,{name:'renamed'}));
  assert.equal(published.length,2,'a real change is still written');
  await ctx.persistArmadaMembership(api,room(2));
  assert.equal(published.length,3,'a new room is still written');
  await ctx.leaveArmadaMembership(api,room(2));
  assert.equal(published.length,4,'leaving is still written');
  await ctx.persistArmadaMembership(api,room(2));
  assert.equal(published.length,5,'re-joining after a leave is a change, and is written');
  console.log('membership vault: written only on change');
}

// ------------------------------------------------- shared DOM-less load of the whole module
const noop=()=>{};
const store={};
const localStorage={getItem:k=>(k in store?store[k]:null),setItem:(k,v)=>{store[k]=String(v);},removeItem:k=>{delete store[k];}};
const document={querySelector:()=>null,querySelectorAll:()=>[],createElement:()=>({dataset:{}}),head:{appendChild:noop},
  documentElement:{appendChild:noop},addEventListener:noop,body:{classList:{add:noop,remove:noop,contains:()=>false}}};
const window={document,addEventListener:noop};
const mod={window,document,console,setTimeout:()=>0,clearTimeout:noop,URL,atob,crypto:{},localStorage,sessionStorage:{getItem:()=>null,setItem:noop}};
vm.runInNewContext(source,mod);
const C=window.PCConcord;

// ---------------------------------------------------------------- 2. listing announcement
{
  assert.equal(typeof C.announceListing,'function','announceListing must be exported for the listing button');
  let signed=0;const sent=[];
  const p={publish:async(kind,content,tags)=>{signed++;return {ok:true,ev:{id:'ann'+signed,kind,content,tags,pubkey:p.viewer().pubkey,created_at:1000+signed,sig:'s'}};},
           relayPublishTo:async(urls,ev)=>{sent.push([urls,ev.id]);return true;},viewer:()=>({pubkey:'a'.repeat(64)})};
  const room={name:'Lounge',url:'https://armada.buzz/invite/naddr1xyz#secret'};
  const first=await C.announceListing(p,room);
  const second=await C.announceListing(p,room);
  const third=await C.announceListing(p,room);
  assert.equal(signed,1,'listing twice signed '+signed+' identical kind-1 notes — the exact Amethyst spam hash');
  assert.equal(first.id,second.id);assert.equal(second.id,third.id);
  assert.equal(sent.length,3,'the same signed announcement is re-sent each time');
  assert(sent.every(([,id])=>id===first.id));
  // Another account on the same device signs its own announcement once.
  p.viewer=()=>({pubkey:'b'.repeat(64)});
  await C.announceListing(p,room);await C.announceListing(p,room);
  assert.equal(signed,2,'a second account gets exactly one announcement of its own');
  // A different room is a different announcement.
  await C.announceListing(p,{name:'Other',url:'https://armada.buzz/invite/naddr1abc#s'});
  assert.equal(signed,3);
  console.log('listing: one signed announcement, re-sent');
}

// ---------------------------------------------------------------- 3. poll re-vote
{
  assert.equal(typeof C.pollVoteChanged,'function','pollVoteChanged must be exported');
  const me='c'.repeat(64);
  const poll={multi:false,votes:[{pubkey:me,optionIds:['a'],ms:10},{pubkey:'d'.repeat(64),optionIds:['b'],ms:11}]};
  assert.equal(C.pollVoteChanged(poll,me,['a']),false,'repeating your current single-choice answer is not a vote');
  assert.equal(C.pollVoteChanged(poll,me,['b']),true);
  assert.equal(C.pollVoteChanged({multi:true,votes:[{pubkey:me,optionIds:['a','b'],ms:3}]},me,['b','a']),false,'order does not make a multi-choice answer new');
  assert.equal(C.pollVoteChanged({multi:true,votes:[]},me,[]),false,'withdrawing from nothing is nothing');
  assert.equal(C.pollVoteChanged({multi:false,votes:[]},me,['a']),true,'a first vote is a vote');
  console.log('polls: unchanged answers publish nothing');
}

// ---------------------------------------------------------------- 4. webxdc stays in the room
{
  const src=slice('  async function webxdcPublish(','  async function webxdcSubscribe(');
  assert(!/p\.relayPublish\(made\.wrap\)/.test(src),
    'webxdcPublish still sends the room envelope to the user\'s general relay pool (public relays)');
  console.log('webxdc: room relays only');
}
console.log('no duplicate publishes passed');
