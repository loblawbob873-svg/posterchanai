/* "Concord live sync failed Error: channel is not readable with this membership", for ever.
 *
 * Reported with the whole stack, from a LIVE TICK — which is the part that matters. `room.channels`
 * is saved to localStorage and `applyControl` only replaces it when a control fetch yields
 * something, so a room can keep a channel the community has since renamed or removed, or one a
 * partial fetch once produced. The live tick picks that channel BY NAME out of the saved list and
 * hands its id to the reader, which builds its readable set from the control events and from
 * nothing else — so it refuses, correctly. The message reads as a permissions problem and is a
 * bookkeeping one, and on a timer it never stops: the room never syncs for the whole visit.
 *
 * This drives the real `readChat` against a reader that knows one channel and a saved room that
 * names another.
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
  body:{classList:{add:noop, remove:noop, contains:c=>c==='concord-view'}}};
const window = {document, addEventListener:noop};

const BUNDLE = {community_id:'a'.repeat(64), channels:[], relays:['wss://r.example']};
// THE SAVED ROOM NAMES A CHANNEL THE COMMUNITY NO LONGER HAS.
const STALE = {name:'general', id:'chan-OLD', streamPubkeys:['b'.repeat(64)]};
const ROOM = {protocol:'cord', name:'PosterChan', communityId:'cid-1', naddr:'cid-1',
  channels:[STALE], cord:{bundle:BUNDLE}};
store['pc.concord.invites'] = JSON.stringify([ROOM]);

vm.runInNewContext(src, {window, document, console, setTimeout:()=>0, clearTimeout:noop,
  URL, atob, crypto:{}, localStorage, sessionStorage:{getItem:()=>null, setItem:noop}});
const api = window.PCConcord;

const LIVE = [{id:'chan-NEW', name:'general', private:false, streamPubkeys:['b'.repeat(64)]}];
const asked = [];
const reader = {
  inspectControl: () => ({name:'PosterChan', description:'', banned:[], channels:LIVE,
                          controlPubkeys:['b'.repeat(64)]}),
  inspectChat: async (bundle, wraps, id) => {
    asked.push(id);
    if (!LIVE.some(c => c.id === id))
      throw new Error('channel is not readable with this membership');
    return {messages:[{id:'m1', pubkey:'c'.repeat(64), text:'hello', at:1, kind:9, tags:[]}],
            reactions:[], reactionUrls:[]};
  },
};
const toasts = [];
const p = {toast: t => toasts.push(String(t))};

const opened = await api.readChat(p, reader, BUNDLE, [{id:'ctrl-1'}], ROOM, STALE, []);

if (!(opened && (opened.messages || []).length))
  throw new Error('a stale channel name still lost the whole channel');
if (!asked.includes('chan-NEW'))
  throw new Error('it never retried against the channel the community actually has: ' + asked.join(','));

/* THE REPAIR IS AN ID LOOKUP FOR THIS READ. IT MUST NOT REWRITE THE ROOM.
 *
 * It used to replace room.channels with whatever THIS control read yielded, and persist that. A
 * control read is often PARTIAL — some events back, not all, the ordinary case on a slow relay — so
 * one stale id was enough to delete every channel that read did not mention. Those channels then
 * reported "channel is not readable with this membership" for ever and the community looked empty. */
const after = JSON.parse(localStorage.getItem('pc.concord.invites'))[0];
if ((after.channels || []).length !== 1 || after.channels[0].id !== 'chan-OLD')
  throw new Error('the repair rewrote the saved channel list: ' + JSON.stringify(after.channels));

/* AND A PARTIAL CONTROL SET MUST NOT COST A ROOM ITS OTHER CHANNELS. */
{
  const many = {protocol:'cord', name:'Big', communityId:'cid-many', naddr:'cid-many',
    channels:[{name:'general', id:'g1', streamPubkeys:['b'.repeat(64)]},
              {name:'random',  id:'r1', streamPubkeys:['b'.repeat(64)]},
              {name:'dev',     id:'d1', streamPubkeys:['b'.repeat(64)]}],
    cord:{bundle:BUNDLE}};
  store['pc.concord.invites'] = JSON.stringify([many]);
  const partial = {
    /* Only ONE channel came back this time. */
    inspectControl: () => ({name:'Big', channels:[{id:'g2', name:'general', streamPubkeys:['b'.repeat(64)]}],
                            controlPubkeys:['b'.repeat(64)]}),
    inspectChat: async (b, w, id) => {
      if (id !== 'g2') throw new Error('channel is not readable with this membership');
      return {messages:[], reactions:[], reactionUrls:[], pollVotes:[]};
    },
  };
  await api.readChat(p, partial, BUNDLE, [{id:'ctrl-1'}], many, many.channels[0], []);
  const kept = JSON.parse(localStorage.getItem('pc.concord.invites'))[0];
  if ((kept.channels || []).length !== 3)
    throw new Error('a partial control read deleted channels: ' + JSON.stringify(kept.channels));
}

/* AND A GENUINELY UNREADABLE COMMUNITY STILL SAYS SO. An empty control set is "could not ask" —
   repairing from it would replace a real channel list with nothing. */
const blind = {inspectControl: () => ({channels: [], controlPubkeys: []}),
               inspectChat: async () => { throw new Error('channel is not readable with this membership'); }};
let threw = null;
try { await api.readChat(p, blind, BUNDLE, [{id:'ctrl-1'}], ROOM, STALE, []); }
catch (e) { threw = e; }
if (!threw) throw new Error('an unreadable community was reported as fine');

/* …and it did NOT overwrite the channel list on the way past. */
const after2 = JSON.parse(localStorage.getItem('pc.concord.invites'))[0];
if (!(after2.channels || []).length)
  throw new Error('a control set that could not be read emptied the saved channel list');

console.log('concord stale channel runtime ok');
