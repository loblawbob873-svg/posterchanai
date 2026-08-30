/* A WALL OF THE SAME ERROR IS NOT A LOG.
 *
 * refreshActiveChannel runs on a four-second timer, so a condition that persists — a control stream
 * the relay has not answered for yet, ordinary on a phone — printed the same line for ever.
 * Reported from Android as a wall of "channel is not readable with this membership".
 */
import fs from 'fs';
import vm from 'vm';
const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const noop = () => {};
const store = {};
const localStorage = {getItem:k=>(k in store?store[k]:null), setItem:(k,v)=>{store[k]=String(v);},
                     removeItem:k=>{delete store[k];}};
const document = {querySelector:()=>null, querySelectorAll:()=>[], createElement:()=>({dataset:{}}),
  head:{appendChild:noop}, documentElement:{appendChild:noop}, addEventListener:noop,
  body:{classList:{add:noop, remove:noop, contains:c=>c==='concord-view'}}};
const window = {document, addEventListener:noop};
const warns = [];
const console2 = {...console, warn:(...a)=>{warns.push(a.map(String).join(' '));}};
vm.runInNewContext(src, {window, document, console:console2, setTimeout:(f)=>{f();return 0;},
  clearTimeout:noop, URL, atob, crypto:{}, localStorage, sessionStorage:{getItem:()=>null,setItem:noop}});
const api = window.PCConcord;

const CH = {name:'general', id:'c1', streamPubkeys:['b'.repeat(64)]};
const BUNDLE = {community_id:'a'.repeat(64), channels:[], relays:['wss://r.example']};
const ROOM = {protocol:'cord', name:'Room', communityId:'cid-1', naddr:'cid-1',
              channels:[CH], cord:{bundle:BUNDLE}};
store['pc.concord.invites'] = JSON.stringify([ROOM]);
api.__testState({community:0, channel:'general', controls:['cid-1', [{id:'ctrl-1'}]]});

window.PosterCordReader = {
  inspectControl: () => ({name:'Room', description:'', banned:[], channels:[], controlPubkeys:[]}),
  inspectChat: async () => { throw new Error('channel is not readable with this membership'); },
};
const p = {
  toast: noop, profOf: () => ({}), enc: s => String(s), $: () => null,
  viewer: () => ({pubkey:'c'.repeat(64), profile:{}}), isView: () => true,
  relaySubscribe: () => ({close(){}}),
  relayQuery: async () => [], relayQueryFrom: async () => [], verifyRelayEvents: async e => e,
};

/* Six ticks of the SAME persistent failure. */
for (let i = 0; i < 6; i++) await api.refreshActiveChannel(p);
const same = warns.filter(w => /not readable with this membership/.test(w));
if (!same.length) throw new Error('the failure was never reported at all — silence is worse');
if (same.length > 1)
  throw new Error('the same failure was printed ' + same.length + ' times in six ticks');

/* A DIFFERENT failure on the same channel is still worth printing. */
window.PosterCordReader.inspectChat = async () => { throw new Error('relay went away'); };
await api.refreshActiveChannel(p);
if (!warns.some(w => /relay went away/.test(w)))
  throw new Error('a new, different failure was swallowed by the de-duplication');

console.log('concord warn once runtime ok');
