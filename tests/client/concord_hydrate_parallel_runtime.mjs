/* A ROOM IS NOT ONE SERIAL QUEUE, AND ONE BAD CHANNEL IS NOT A BAD ROOM.
 *
 * Every channel waited on the previous channel's relay round trip, so a ten-channel community cost
 * ten of them before the room was usable — reported as Concord being "slow as fuck", worst on the
 * big Armada rooms. And a single throw in that loop abandoned every channel behind it, surfaced as
 * "could not refresh room history", and left `hydrated` unset, so the whole room was fetched again
 * on the next click: the next failure likelier, the room slower still.
 *
 * This drives the real hydrateRoomStreams.
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
  body:{classList:{add:noop, remove:noop, contains:()=>false}}};
const window = {document, addEventListener:noop};
vm.runInNewContext(src, {window, document, console, setTimeout:(f,ms)=>{queueMicrotask(f);return 0;},
  clearTimeout:noop, URL, atob, crypto:{}, localStorage, sessionStorage:{getItem:()=>null, setItem:noop}});
const api = window.PCConcord;

/* Each channel gets its OWN stream key, so a fake relay can refuse exactly one of them — which is
   the case that matters: the channel on screen is slow, the rest are fine. */
const KEY = n => String(n).repeat(64).slice(0, 64);
const CH = n => ({name:'c'+n, id:'id'+n, streamPubkeys:[KEY(n)]});
const CHANNELS = [CH(0), CH(1), CH(2), CH(3), CH(4), CH(5)];
const BUNDLE = {community_id:'a'.repeat(64), channels:[], relays:['wss://r.example']};
const ROOM = {protocol:'cord', name:'Armada Room', communityId:'cid-1', naddr:'cid-1',
              channels:[CH(0)], cord:{bundle:BUNDLE}};
store['pc.concord.invites'] = JSON.stringify([ROOM]);
api.__testState({community:0, channel:'c0'});

window.PosterCordReader = {
  inspectControl: () => ({name:'Armada Room', description:'', banned:[], channels:CHANNELS,
                          controlPubkeys:['b'.repeat(64)]}),
  inspectChat: async () => ({messages:[], reactions:[], reactionUrls:[], pollVotes:[]}),
};

let inFlight = 0, peak = 0;
const asked = [];
let failFirst = false;   // the channel on screen is the one the relay will not answer for
const p = {
  toast: noop, profOf: () => ({}), enc: s => String(s), $: () => null,
  viewer: () => ({pubkey:'c'.repeat(64), profile:{}}), isView: () => true,
  // A subId string + relayClose, matching app.js's real PC surface (see
  // tests/client/test_relay_subscription_contract.py — a fake with a contract the real module does
  // not have is how the never-closed Concord subscriptions stayed invisible).
  relaySubscribe: () => 'sub-hydrate',
  relayClose: () => {},
  /* cordQuery asks the shared pool AND the room's own relays, and only fails when BOTH do — so a
     fixture whose pool always answers can never make a channel fail. In the failFirst scenario the
     pool is down too, which is what a phone on a bad connection actually looks like. */
  relayQuery: async (filters) => {
    const authors = (filters && filters[0] && filters[0].authors) || [];
    if (failFirst && authors.includes(KEY(0))) throw new Error('pool down');
    return [];
  },
  relayQueryFrom: async (relays, filters) => {
    const authors = (filters && filters[0] && filters[0].authors) || [];
    asked.push(authors.join(','));
    inFlight++; peak = Math.max(peak, inFlight);
    await new Promise(r => setTimeout(r, 5));
    inFlight--;
    /* ONE CHANNEL THE RELAY WILL NOT ANSWER FOR. */
    /* Refuse ONE named channel — the one on screen — rather than everything. cordQuery only fails
       when the pool AND the room relays both do, so the pool refuses that same channel below. */
    if (failFirst && authors.includes(KEY(0))) throw new Error('relay refused');
    if (!failFirst && asked.length === 3) throw new Error('relay refused');
    return [];
  },
  verifyRelayEvents: async e => e,
};

await api.hydrateRoomStreams(p, 0, 'cid-1');

/* THE ROOM SURVIVED THE BAD CHANNEL. */
const after = JSON.parse(localStorage.getItem('pc.concord.invites'))[0];
if (!after) throw new Error('the room vanished when one channel failed');
if (!(after.channels || []).length)
  throw new Error('the room lost its channels when one of them failed');

/* AND IT WAS NOT ONE SERIAL QUEUE. The head is fetched alone on purpose; everything after it
   overlaps, so a room with six channels must have had more than one request in the air. */
if (peak < 2)
  throw new Error('channel history is still fetched one at a time (peak in flight: ' + peak + ')');

/* EVERY CHANNEL WAS ASKED FOR, not just the ones before the failure. */
if (asked.length < CHANNELS.length)
  throw new Error('a failing channel abandoned the ones behind it: asked ' + asked.length +
                  ' of ' + CHANNELS.length);

/* THE CHANNEL ON SCREEN IS THE ONE THAT FAILS — the ordinary case on a phone, where one relay past
   the six-second window is enough. It used to throw out of hydrateRoomStreams, which produced BOTH
   halves of the APK report at once: the toast said "could not refresh room history" and the room
   showed no messages, because every channel behind it was abandoned and the control set was never
   applied. */
{
  store['pc.concord.invites'] = JSON.stringify([{...ROOM, communityId:'cid-2', naddr:'cid-2'}]);
  api.__testState({community:0, channel:'c0'});
  failFirst = true; asked.length = 0;
  let threw = null;
  try { await api.hydrateRoomStreams(p, 0, 'cid-2'); } catch (e) { threw = e; }
  if (threw)
    throw new Error('one slow channel still took the whole room: ' + threw.message);
  if (asked.length < CHANNELS.length)
    throw new Error('the channels behind the failing one were abandoned: asked ' + asked.length);
  const saved2 = JSON.parse(localStorage.getItem('pc.concord.invites'))[0];
  if (!(saved2.channels || []).length)
    throw new Error('the room lost its channels when the open one failed');
  if (!(saved2.cord.stalled || []).includes('c0'))
    throw new Error('the channel that failed was not recorded as stalled: ' +
                    JSON.stringify(saved2.cord.stalled));
}

/* …AND A ROOM WHERE NOTHING CAN BE READ STILL SAYS SO. Silence there is a community that is simply
   empty on screen with no explanation, which is the other half of the same report. */
{
  store['pc.concord.invites'] = JSON.stringify([{...ROOM, communityId:'cid-3', naddr:'cid-3'}]);
  api.__testState({community:0, channel:'c0'});
  asked.length = 0;
  /* BOTH transports down. cordQuery fails only when the pool and the room's relays both do; a
     pool that answers with an empty list is a relay saying "nothing here", which is a different
     thing and must not raise anything. */
  const dead = {...p, relayQuery: async () => { throw new Error('pool down'); },
                      relayQueryFrom: async () => { throw new Error('relay down'); }};
  let threw2 = null;
  try { await api.hydrateRoomStreams(dead, 0, 'cid-3'); } catch (e) { threw2 = e; }
  if (!threw2) throw new Error('a room that could read nothing at all reported success');
}

/* OPENING A ROOM MUST NOT WAIT FOR EVERY CHANNEL IN IT.
 *
 * Only one channel is being looked at. Awaiting the rest made a thirteen-channel community cost
 * thirteen relay round trips before it was usable, while a one-channel room was ready in five
 * seconds — the "Concord is slow" report, worst on exactly the big Armada rooms.
 *
 * Nothing is lost: refreshActiveChannel fetches whatever channel is actually open on its next tick,
 * and with no prior messages its `since` is 0, so an un-prefetched channel fills the moment
 * somebody opens it. */
{
  store['pc.concord.invites'] = JSON.stringify([{...ROOM, communityId:'cid-4', naddr:'cid-4'}]);
  api.__testState({community:0, channel:'c0'});
  failFirst = false; asked.length = 0;
  const slow = {...p, relayQueryFrom: async (relays, filters) => {
    const authors = (filters && filters[0] && filters[0].authors) || [];
    asked.push(authors.join(','));
    /* Only the PREFETCHED channels are slow. The control stream and the channel on screen are
       legitimately blocking — a room cannot open without them — so making them slow here would
       measure the fixture rather than the change. */
    const prefetched = CHANNELS.slice(1).some(c => authors.includes(c.streamPubkeys[0]));
    await new Promise(r => setTimeout(r, prefetched ? 400 : 5));
    return [];
  }};
  const t0 = Date.now();
  await api.hydrateRoomStreams(slow, 0, 'cid-4');
  const openedIn = Date.now() - t0;
  const fetchedByThen = asked.length;
  if (fetchedByThen >= CHANNELS.length + 1)
    throw new Error('opening the room waited for every channel (' + fetchedByThen + ' fetches, ' +
                    openedIn + 'ms) — a big community is unusable until all of them answer');
  if (openedIn > 350)
    throw new Error('opening the room took ' + openedIn + 'ms; only the open channel is needed');
}

console.log('concord hydrate parallel runtime ok');
