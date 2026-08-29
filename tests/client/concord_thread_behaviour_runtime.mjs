/* HOW A THREAD BEHAVES, driven rather than described.
 *
 * The pieces existed — threadIndex/threadRootId/threadView, the "N replies" button, the thread bar
 * with its Back — but nothing had ever run them against the shapes a real channel produces: a
 * reply to a reply, a reply whose parent was never fetched, two threads side by side, and a
 * malformed `e` chain from a relay that owes us nothing.
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
vm.runInNewContext(src, {window, document, console, setTimeout:()=>0, clearTimeout:noop,
  URL, atob, crypto:{}, localStorage, sessionStorage:{getItem:()=>null, setItem:noop}});
const api = window.PCConcord;

const msg = (id, at, parent) =>
  ({id, at, pubkey:'a'.repeat(64), by:'someone', text:'m'+id, kind: parent ? 1111 : 9,
    tags: parent ? [['e', parent, '', 'a'.repeat(64)]] : []});

/* A REPLY TO A REPLY BELONGS TO THE SAME THREAD. Grouped by immediate parent instead of root, a
   conversation three deep becomes three threads of one and the "N replies" count reads 1 for
   everything. */
const rows = [msg('root', 1), msg('r1', 2, 'root'), msg('r2', 3, 'r1'), msg('r3', 4, 'r2')];
const idx = api.threadIndex(rows);
if ((idx.get('root') || []).length !== 3)
  throw new Error('a nested reply did not join its thread: ' + JSON.stringify([...idx]));

/* THE VIEW IS THE ROOT THEN ITS DESCENDANTS, IN TIME ORDER. */
const view = api.threadView(rows, 'root').map(m => m.id);
if (view.join(',') !== 'root,r1,r2,r3')
  throw new Error('the thread did not read in order: ' + view.join(','));

/* TWO THREADS DO NOT BLEED INTO EACH OTHER. */
const two = [...rows, msg('root2', 5), msg('x1', 6, 'root2')];
if (api.threadView(two, 'root2').map(m => m.id).join(',') !== 'root2,x1')
  throw new Error('a second thread picked up the first thread\'s replies');
if (api.threadView(two, 'root').length !== 4)
  throw new Error('the first thread changed when a second one appeared');

/* A REPLY WHOSE PARENT WAS NEVER FETCHED IS NOT A THREAD OF ITS OWN. Backfill is partial by
   nature; treating an unresolved parent as a root would scatter one conversation across the
   channel as a row of single-reply threads. */
const orphan = api.threadIndex([msg('lonely', 9, 'never-fetched')]);
if (orphan.size !== 0)
  throw new Error('a reply with an unfetched parent invented a thread: ' + JSON.stringify([...orphan]));

/* A MALFORMED CHAIN MUST NOT SPIN. Anyone can publish an event; two messages naming each other as
   parent is a free hang otherwise. */
const cyclic = [{...msg('a', 1), tags:[['e','b','','x']]}, {...msg('b', 2), tags:[['e','a','','x']]}];
const t0 = Date.now();
api.threadIndex(cyclic);
api.threadRootId(cyclic, cyclic[0]);
if (Date.now() - t0 > 1000) throw new Error('a cyclic reply chain hung the thread walk');

/* AN EMPTY THREAD IS NOT AN EMPTY CHANNEL. Opening a thread whose root has not arrived yet used
   to return [] straight into the message list, which rendered as a community with no messages at
   all — reported as "my posterchan concord community just disappeared". */
if (api.threadView(rows, 'not-here').length !== 0)
  throw new Error('a missing root produced messages from somewhere');
if (!/if\(!_t\.length\)\{ state\.thread=null; return messages; \}/.test(src))
  throw new Error('the empty-thread fallback is gone — an unarrived root blanks the channel again');

/* A REPLY TAGS EVERYONE ALREADY IN THE THREAD — that is what turns a reply into a notification
   for the people having the conversation, and it is why the tagging walks the whole thread rather
   than the selected message's ancestors: replying to the ROOT after a few branches exist would
   otherwise silently leave out everyone in them. You are never tagged into your own reply. */
const other = 'b'.repeat(64);
const mixed = [...rows, {...msg('r4', 7, 'root'), pubkey: other}];
const who = api.threadParticipants(mixed, mixed[mixed.length - 1], other);
if (!who.includes('a'.repeat(64)))
  throw new Error('a reply does not tag the people already in the thread: ' + JSON.stringify(who));
if (who.includes(other))
  throw new Error('the replier tagged themselves: ' + JSON.stringify(who));

console.log('concord thread behaviour runtime ok');
