/* BOOKMARKS ARE THE ONE VIEW THAT IS CERTAIN TO BE CACHED.
 *
 * You bookmarked those posts by LOOKING at them, so the Store holds them — and the view was written
 * as a spinner in front of a network chain: blank the feed, await the relay, paint at the bottom.
 * Reported as "taking forever to load, circle black screen".
 *
 * Two independent reasons it could sit there for ever, and both are driven here:
 *   1. the REQ went out without `Relay.ready()`, and a REQ written to a CONNECTING socket is
 *      dropped in silence, so nothing ever answers and nothing ever repaints;
 *   2. the paint was behind the fetch even for posts already held, so ONE missing id hid every
 *      bookmark that was right there.
 *
 * This extracts the shipped renderBookmarks and runs it with a relay that never answers.
 */
import fs from 'node:fs';
const app = fs.readFileSync(new URL('../../static/js/client/app.js', import.meta.url), 'utf8');
const start = app.indexOf('  async function renderBookmarks(){');
const end = app.indexOf('  // ---------- minimal, SAFE markdown renderer', start);
if (start < 0 || end < 0) throw new Error('renderBookmarks moved');
const shipped = app.slice(start, end);

const held = new Map([['a1', {id:'a1', pubkey:'p1', created_at:2}],
                      ['a2', {id:'a2', pubkey:'p2', created_at:1}]]);
globalThis.BOOKMARKS = new Set(['a1', 'a2', 'missing1']);
globalThis.VIEW = 'bookmarks';
globalThis.Store = { get: id => held.get(id) || null, saveEvent(){} };
globalThis.needProfile = () => {};
globalThis.noteHtml = ev => `<article data-id="${ev.id}"></article>`;
globalThis.hydrate = () => {};
const feed = { innerHTML: '' };
globalThis.$ = sel => sel === '#feed' ? feed : null;

const seen = [];
let readyCalled = false, queryCalled = false;
globalThis.Relay = {
  async ready(){ readyCalled = true; seen.push('ready'); return true; },
  /* A relay that NEVER answers — the "connecting socket" case, which is the whole point. */
  query(){ queryCalled = true; seen.push('query'); return new Promise(() => {}); },
};

const done = globalThis.renderBookmarks ? null : null;
const fn = new Function(shipped + '\n;return renderBookmarks;')();
const running = fn();

/* Let the synchronous part and one microtask turn go by, then look: the held posts must ALREADY be
   on screen even though the relay has answered nothing and never will. */
await new Promise(r => setTimeout(r, 30));

process.stdout.write(JSON.stringify({
  paintedWhileRelayHangs: (feed.innerHTML.match(/<article/g) || []).length,
  isSpinner: feed.innerHTML.includes('spinner'),
  readyCalled, queryCalled,
  readyBeforeQuery: seen.indexOf('ready') !== -1 && seen.indexOf('ready') < seen.indexOf('query'),
}));
process.exit(0);
