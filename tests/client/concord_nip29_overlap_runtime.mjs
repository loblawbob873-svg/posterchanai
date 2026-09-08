/* MESSAGES ARRIVE, THEY ARE NOT FETCHED FOR.
 *
 * The live tick runs every four seconds, but the query it makes carries `minInterval:60000` — so on
 * any community whose relays are not already in the shared pool a message could take a FULL MINUTE
 * to appear. Tightening the timer cannot fix that; polling a relay that can push is the wrong shape.
 *
 * This drives the real startChatLive/flushChatLive with a fake subscription: an event is pushed,
 * and the channel's store must contain it without any query having been made.
 */
import fs from 'fs';
import vm from 'vm';
const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const noop = () => {};
const store = {};
const localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
const document = {querySelector:()=>null, querySelectorAll:()=>[], createElement:()=>({dataset:{}}),
  head:{appendChild:noop}, documentElement:{appendChild:noop}, addEventListener:noop,
  /* NOT on screen. What is under test is that a PUSHED wrap is opened once, as a batch, and
     lands in the channel's store — the repaint half is the render suite's job, and driving it
     here would only mean stubbing the whole DOM to watch a store write. */
  body:{classList:{add:noop, remove:noop, contains:()=>false}}};
const window = {document, addEventListener:noop};

const BUNDLE = {community_id:'a'.repeat(64), channels:[], relays:['wss://r.example']};
const CH = {name:'general', id:'chan-1', streamPubkeys:['b'.repeat(64)]};
const ROOM = {protocol:'cord', name:'PosterChan', communityId:'cid-1', naddr:'cid-1',
              channels:[CH], cord:{bundle:BUNDLE}};
store['pc.concord.invites'] = JSON.stringify([ROOM]);

const timers = [];
vm.runInNewContext(src, {window, document, console,
  setTimeout:(f,ms)=>{timers.push(f);return timers.length;}, clearTimeout:noop,
  URL, atob, crypto:{}, localStorage, sessionStorage:{getItem:()=>null, setItem:noop}});
const api = window.PCConcord;

let opened = [];
window.PosterCordReader = {
  inspectControl: () => ({name:'PosterChan', description:'', banned:[], channels:[CH],
                          controlPubkeys:['b'.repeat(64)]}),
  inspectChat: async (bundle, ctrl, id, wraps) => {
    opened.push((wraps || []).length);
    return {messages:(wraps||[]).map((w,i)=>({id:'m'+w.id, pubkey:'c'.repeat(64),
              text:'pushed '+w.id, at:1000+i, kind:9, tags:[]})),
            reactions:[], reactionUrls:[]};
  },
};

let subFilters = null, subLive = null, onEvent = null, externalEvent = null, closed = 0, externalClosed = 0;
const externalEvents=[];
window.Relay = {
  subscribe: (filters, h) => { subFilters=filters;subLive=h&&h.live;onEvent=h&&h.onEvent;return 'real-string-sub-id'; },
  subscribeFrom: (urls, filters, h) => { externalEvent=h&&h.onEvent;externalEvents.push(externalEvent);const close=()=>{externalClosed++;};close.hasTargets=true;close.ready=Promise.resolve(true);return close; },
  waitForSubscription: async () => false,
  close: id => { if(id!=='real-string-sub-id')throw new Error('wrong managed subscription id');closed++; },
};
const p = {
  relayQuery: async () => { throw new Error('the live path must not poll'); },
  relayQueryFrom: async () => { throw new Error('the live path must not poll'); },
  verifyRelayEvents: async e => e, profOf: () => ({}), toast: noop,
  viewer: () => ({pubkey:'c'.repeat(64), profile:{}}), enc: s => String(s), $: () => null,
};

api.__testState({community:0, channel:'general', controls:['cid-1', [{id:'ctrl-1'}]]});
api.startChatLive(p, ROOM, CH);

if (!subFilters) throw new Error('chat never subscribed — it is still polling');
if (subLive !== true) throw new Error('the chat subscription is not live');
const f = subFilters[0];
if (!(f.kinds || []).includes(1059))
  throw new Error('the subscription does not ask for the channel wrap kind: ' + JSON.stringify(f));
if (String((f.authors || [])[0]) !== 'b'.repeat(64))
  throw new Error('the subscription is not scoped to the channel stream: ' + JSON.stringify(f));

/* A BURST IS ONE BATCH. Opening a wrap is real cryptography; per event on the main thread is how a
   busy channel becomes a stutter. */
/* Deliver on the invite relay handle. The managed pool deliberately reports not-ready above. */
externalEvent({id:'w1', kind:1059});
externalEvent({id:'w2', kind:1059});
externalEvent({id:'w3', kind:1059});
if (timers.length !== 1)
  throw new Error('three arrivals scheduled ' + timers.length + ' flushes, not one');
onEvent({id:'ignored', kind:1});          // not a wrap
/* The timer callback fires the flush and returns — a setTimeout callback cannot be awaited, so
   the module launches it with `void`. Let the async chain settle before reading the store. */
timers[0]();
for (let i = 0; i < 20; i++) await new Promise(r => setImmediate(r));

if (opened.length !== 1) throw new Error('the batch was opened ' + opened.length + ' times');
if (opened[0] !== 3) throw new Error('the batch carried ' + opened[0] + ' wraps, not 3');

const saved = api.__testMessages('cid-1');
const texts = (saved || []).map(m => m.text).sort();
if (texts.length !== 3)
  throw new Error('pushed messages did not reach the channel store: ' + JSON.stringify(saved));

/* SWITCHING CHANNELS CLOSES THE OLD STREAM, or the channel you left feeds the one you opened. */
api.startChatLive(p, ROOM, {name:'other', id:'chan-2', streamPubkeys:['d'.repeat(64)]});
if (!closed) throw new Error('switching channels left the old subscription open');
if (!externalClosed) throw new Error('switching channels left the external room socket open');
const timerCount=timers.length;
externalEvents[0]({id:'late-old-room',kind:1059});
if(timers.length!==timerCount)throw new Error('a stale room callback scheduled a flush after close');

/* NIP-29 uses public group events rather than encrypted kind-1059 wraps, but it needs the same
   managed+invite-relay lifecycle and must update without leaving and re-entering the room. */
const NIP={protocol:'nip29',name:'Public group',naddr:'nip-room',groupId:'group-1',relay:'wss://groups.example',
           nip29Hydrated:true,channels:[{name:'general',id:'general'}]};
store['pc.concord.invites']=JSON.stringify([NIP]);
api.__testState({community:0,channel:'general'});
api.startChatLive(p,NIP,NIP.channels[0]);
const nipFilter=subFilters[0];
for(const kind of [5,7,9,10,11,12,1111])if(!(nipFilter.kinds||[]).includes(kind))
  throw new Error('NIP-29 live filter omitted kind '+kind);
if(JSON.stringify(nipFilter['#h'])!==JSON.stringify(['group-1']))
  throw new Error('NIP-29 live filter is not scoped to its group');
store['pc.concord.test.nip-room']=JSON.stringify([{id:'restored-before-live',pubkey:'e'.repeat(64),kind:9,at:1000,text:'restored history',remote:true,tags:[['h','group-1']],reactions:{}}]);
externalEvent({id:'nip-msg',pubkey:'e'.repeat(64),kind:9,created_at:2,content:'live nip29',tags:[['h','group-1']]});
timers[timers.length-1]();
for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));
const nipSaved=api.__testMessages('nip-room');
if(!nipSaved.some(m=>m.id==='nip-msg'&&m.text==='live nip29'))
  throw new Error('pushed NIP-29 message did not reach its store: '+JSON.stringify(nipSaved));

console.log('concord live messages runtime ok');

let releaseNipHistory;
p.relayQueryFrom=()=>new Promise(resolve=>{releaseNipHistory=resolve});
const original={id:'nip-msg',pubkey:'e'.repeat(64),kind:9,created_at:2,content:'live nip29',tags:[['h','group-1']]};
const reaction={id:'reaction-delayed',pubkey:'c'.repeat(64),kind:7,created_at:3,content:'+',tags:[['h','group-1'],['e','nip-msg']]};
externalEvent(reaction);timers.at(-1)();
for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));
externalEvent({id:'newer-nip-msg',pubkey:'e'.repeat(64),kind:9,created_at:4,content:'newer message',tags:[['h','group-1']]});timers.at(-1)();
for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));
releaseNipHistory([original,reaction]);
for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r));
if(!api.__testMessages('nip-room').some(m=>m.id==='newer-nip-msg'))throw Error('late reaction history erased concurrent NIP-29 message');
if(!api.__testMessages('nip-room').some(m=>m.id==='restored-before-live'))throw Error('first live fold dropped restored remote history');
console.log('NIP-29 overlap retained newest message');
const settle=async()=>{for(let i=0;i<20;i++)await new Promise(r=>setImmediate(r))};
const push=async ev=>{externalEvent(ev);timers.at(-1)();await settle()};
const ev=(id,kind,author,at,content='',target='')=>({id,kind,pubkey:author,created_at:at,content,tags:[['h','group-1'],...(target?[['e',target]]:[])]});
// Authorized deletion must remove its target without erasing a concurrent unrelated message.
const del=ev('delete-original',5,original.pubkey,5,'','nip-msg');
await push(del);
await push(ev('newest',9,original.pubkey,6,'identical intentional text'));
await push(ev('distinct-same-text',9,original.pubkey,7,'identical intentional text'));
releaseNipHistory([original,reaction,del]);await settle();
let rows=api.__testMessages('nip-room');
if(rows.some(m=>m.id==='nip-msg'))throw Error('deleted message resurrected');
for(const id of ['newer-nip-msg','newest','distinct-same-text'])if(!rows.some(m=>m.id===id))throw Error('deletion/history erased unrelated distinct signed message '+id);
// An older history result which omits the deletion cannot resurrect its target.
await push(ev('reaction-newer',7,'c'.repeat(64),8,'+','newer-nip-msg'));
releaseNipHistory([original,reaction]);await settle();
if(api.__testMessages('nip-room').some(m=>m.id==='nip-msg'))throw Error('stale history resurrected a previously deleted message');
// A different author cannot delete somebody else's post.
await push(ev('forged-delete',5,'f'.repeat(64),9,'','newest'));
releaseNipHistory([]);await settle();
if(!api.__testMessages('nip-room').some(m=>m.id==='newest'))throw Error('wrong-author deletion removed a message');
// History arriving after an account switch must not add rows or restore old stream state.
let owner='c'.repeat(64);p.viewer=()=>({pubkey:owner,profile:{}});
await push(ev('old-owner-reaction',7,owner,10,'+','newest'));
owner='f'.repeat(64);
releaseNipHistory([ev('late-private-row',9,original.pubkey,11,'old account history')]);await settle();
if(api.__testMessages('nip-room').some(m=>m.id==='late-private-row'))throw Error('late history crossed account boundary');
await push(ev('new-account-row',9,owner,12,'new account live'));
rows=api.__testMessages('nip-room');
if(rows.length!==1||rows[0].id!=='new-account-row')throw Error('previous account ledger leaked into new account');
store['pc.concord.test.nip-room']=JSON.stringify([...rows,{id:'pending-own',pubkey:owner,text:'unsent composer row',at:12500,kind:9,pending:true,remote:false}]);
await push(ev('pending-neighbor',9,owner,13,'neighbor live'));
if(!api.__testMessages('nip-room').some(m=>m.id==='pending-own'&&m.pending))throw Error('live fold dropped pending own send');
await push(ev('removed-room-reaction',7,owner,13,'+','new-account-row'));
store['pc.concord.invites']='[]';
releaseNipHistory([ev('removed-room-history',9,owner,14,'removed room')]);await settle();
if(api.__testMessages('nip-room').some(m=>m.id==='removed-room-history'))throw Error('removed room history committed');
console.log('NIP-29 deletion, distinct IDs, owner and membership guards passed');

store['pc.concord.invites']=JSON.stringify([NIP]);
p.relayQueryFrom=async()=>{throw Error('fixture relay unavailable')};
await push(ev('reaction-query-failed',7,owner,15,'+','new-account-row'));
rows=api.__testMessages('nip-room');
if(!rows.some(m=>m.id==='new-account-row'&&Object.values(m.reactions).some(people=>people.includes(owner))))throw Error('failed history query hid known reaction or deleted known message');
p.publishNip29Authed=async(_relay,template)=>({...template,id:'own-ack-before-echo',pubkey:owner});
await api.publishNip29Message(p,NIP,'general','own acknowledged send');
await push(ev('post-ack-neighbor',9,owner,16,'next live event'));
if(!api.__testMessages('nip-room').some(m=>m.id==='own-ack-before-echo'))throw Error('next live fold dropped acknowledged own send before echo');
let rejectHistory;
p.relayQueryFrom=()=>new Promise((_resolve,reject)=>{rejectHistory=reject});
await push(ev('failure-after-switch',7,owner,17,'+','new-account-row'));
owner='a'.repeat(64);rejectHistory(Error('late failed account request'));await settle();
if(api.__testMessages('nip-room').some(m=>m.reactionIds&&Object.values(m.reactionIds).some(ids=>Object.values(ids).includes('failure-after-switch'))))throw Error('failed old-owner query committed fallback after switch');
console.log('NIP-29 restored rows, pending sends, ACK before echo and guarded failure fallback passed');

// Every room in the new account generation must avoid the old owner's global row stores.
await push(ev('account-a-first-room',9,owner,18,'new owner first room'));
const secondRoom={...NIP,naddr:'second-nip-room',groupId:'group-2',channels:[{name:'general',id:'group-2'}]};
store['pc.concord.invites']=JSON.stringify([NIP,secondRoom]);
store['pc.concord.test.second-nip-room']=JSON.stringify([{id:'previous-owner-second-room',pubkey:'f'.repeat(64),kind:9,at:19000,text:'previous owner plaintext',remote:true,tags:[['h','group-2']]}]);
api.__testState({community:1,channel:'general'});api.startChatLive(p,secondRoom,secondRoom.channels[0]);
await push({...ev('new-owner-second-room',9,owner,20,'new owner second room'),tags:[['h','group-2']]});
if(api.__testMessages('second-nip-room').some(m=>m.id==='previous-owner-second-room'))throw Error('second room seeded prior account plaintext');
console.log('NIP-29 two-room account-switch isolation passed');
