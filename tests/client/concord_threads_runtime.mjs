/* Threads in Concord: the presentation half of a protocol that was already right.
 *
 * A reply has always gone out as a kind-1111 with an `e` tag — correct NIP-22, and the same wire
 * format Armada reads — and an incoming one got a single quoted line above the message. That is
 * all: replies were flattened into the channel timeline, so three conversations interleaved and you
 * reconstructed them by eye, and nothing anywhere said a message HAD replies, so a thread was
 * invisible unless you happened to scroll past one of them.
 *
 * These run the SHIPPED helpers. They are DOM-free on purpose — the parent walk is the part worth
 * checking away from a browser, and it is the same walk threadParticipants already does.
 */
import fs from 'fs';
import vm from 'vm';
const src = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
const noop = () => {};
const document = {querySelector:()=>null, querySelectorAll:()=>[], createElement:()=>({dataset:{}}),
  head:{appendChild:noop}, documentElement:{appendChild:noop}, addEventListener:noop,
  body:{classList:{add:noop, contains:()=>false}}};
const window = {document, addEventListener:noop};
vm.runInNewContext(src, {window, document, console, setTimeout:()=>0, clearTimeout:noop, URL, atob,
  crypto:{}, localStorage:{getItem:()=>null, setItem:noop, removeItem:noop},
  sessionStorage:{getItem:()=>null, setItem:noop}});
const api = window.PCConcord;

const msg = (id, at, parent) => ({id, pubkey:'p'+id, by:'u'+id, text:'t'+id, at,
  kind: parent ? 1111 : 9, tags: parent ? [['e', parent]] : [],
  reply: parent ? {id:parent, by:'x', text:'y'} : undefined});

// root ── a ── c   and a second root with none
const rows = [msg('root', 1), msg('a', 2, 'root'), msg('c', 3, 'a'), msg('lonely', 4)];

if (api.replyParentId(rows[1]) !== 'root') throw new Error('an e tag is not read as the parent');
if (api.replyParentId(rows[0]) !== '') throw new Error('a root was given a parent');

/* DEPTH IS NOT ONE. `m.reply` points at the IMMEDIATE parent, so a reply to a reply belonged to
   nothing and showed its parent's text with no sign it sat deeper. The root is what groups them. */
if (api.threadRootId(rows, rows[2]) !== 'root')
  throw new Error('a reply to a reply did not resolve to the root of its thread');

const idx = api.threadIndex(rows);
if ((idx.get('root') || []).length !== 2)
  throw new Error('the root does not know how many replies it has: ' + JSON.stringify([...idx]));
if (idx.has('lonely')) throw new Error('a message with no replies was given a thread');
if (idx.has('a')) throw new Error('a reply was treated as a root — the walk stopped one short');

/* THE VIEW IS THE ROOT PLUS ITS DESCENDANTS, OLDEST FIRST. Anything else and opening a thread
   shows a conversation starting in the middle. */
const view = api.threadView(rows, 'root').map(m => m.id);
if (JSON.stringify(view) !== JSON.stringify(['root', 'a', 'c']))
  throw new Error('thread view is wrong: ' + JSON.stringify(view));
if (api.threadView(rows, 'nope').length !== 0)
  throw new Error('an unknown root produced a thread');

/* A HOSTILE RELAY CAN SEND A CYCLE, and the parent walk must not spin on it. Anyone can publish a
   1111 with any `e` tag; this is a public network. */
const cyclic = [ {...msg('x', 1, 'y')}, {...msg('y', 2, 'x')} ];
const root = api.threadRootId(cyclic, cyclic[0]);
if (!root) throw new Error('a cyclic reply chain produced no root');

/* A REPLY WHOSE PARENT IS NOT IN THIS CHANNEL is its own root, not an orphan hidden from view. */
const orphan = [msg('o', 1, 'missing-parent')];
if (api.threadIndex(orphan).size !== 0)
  throw new Error('a reply to a message we do not have was filed under a thread nobody can open');

console.log('concord threads runtime ok');

/* A THREAD WHOSE ROOT IS NOT IN THE CURRENT MESSAGES MUST NOT EMPTY THE CHANNEL.
 *
 * threadView answers [] for a root it cannot find, and a repaint can happen with a momentarily
 * stale thread id — history reloaded, the room re-hydrated, a live batch replacing the list.
 * Rendered literally that is a community with no messages, which is indistinguishable from the
 * community being gone: "my posterchan concord community just disappeared". */
if (api.threadView(rows, 'not-here').length !== 0)
  throw new Error('threadView invented a thread for a root it does not have');
const guard = fs.readFileSync(new URL('../../static/js/client/concord.js', import.meta.url), 'utf8');
if (!guard.includes("if(!_t.length){ state.thread=null; return messages; }"))
  throw new Error('an empty thread view still empties the channel instead of falling back to it');
console.log('concord threads fallback ok');
