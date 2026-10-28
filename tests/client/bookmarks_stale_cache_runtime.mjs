/* A STALE CACHED LIST IS NOT A LIST A RELAY CONFIRMED.
 *
 * The gate's real-node bookmark test ("both bookmarks listed after a reload", flaky under load for a dozen deploys)
 * dumped the same state every time: after the reload the Store held the OLDER kind-10003 (one bookmark) and the
 * screen listed one post. fetchBookmarks applies the cached list first, and that apply marked the list as read
 * (`_bmReadFor`) -- so when the boot's relay read then timed out ([] with complete:false), renderBookmarks believed
 * the list was confirmed and never asked again. The newer list sat on the relay for the whole session.
 *
 * This runs the SHIPPED _cacheFirstList + fetchBookmarks + renderBookmarks: the Store holds the old list, the relay
 * misses the boot read and answers later. Both bookmarks must end up on screen without a reload.
 */
import { clientSource } from './client_source.mjs';
const app = clientSource();
const cut = (a, b) => { const i = app.indexOf(a), j = app.indexOf(b, i);
  if (i < 0 || j < 0) throw new Error('moved: ' + a); return app.slice(i, j); };
const shipped = [
  cut('  async function _cacheFirstList(', '  // ---- Replaceable-list durability'),
  cut('  async function fetchBookmarks(', '  async function toggleBookmark('),
  cut('  function _bookmarksFromStore(){', '  // ---------- minimal, SAFE markdown renderer'),
  cut('  function _followReadAnswered(', '  async function fetchFollows('),
].join('\n');

const OLD = { id: 'L1', kind: 10003, pubkey: 'me', created_at: 10, tags: [['e', 'a1']] };
const NEW = { id: 'L2', kind: 10003, pubkey: 'me', created_at: 11, tags: [['e', 'a1'], ['e', 'a2']] };
const notes = new Map([['a1', { id: 'a1', pubkey: 'p', created_at: 2 }], ['a2', { id: 'a2', pubkey: 'p', created_at: 1 }]]);
const lists = [OLD];
const feed = { innerHTML: '', querySelector: () => null, insertAdjacentHTML(_, h){ this.innerHTML += h; } };
let listReads = 0;
const answers = ['silent', 'silent', 'answer'];   // the boot read and the view's first re-ask are both missed
const g = {
  BOOKMARKS: new Set(), VIEW: 'home', ME: { pubkey: 'me' }, GUEST: false,
  Store: {
    get: id => notes.get(id) || null,
    saveEvent: e => { if (e.kind === 10003) lists.push(e); else notes.set(e.id, e); },
    query: f => f[0].kinds ? lists.slice().sort((a, b) => b.created_at - a.created_at).slice(0, 1) : [],
  },
  needProfile(){}, hydrate(){}, decorateCounts(){},
  noteHtml: ev => `<article data-id="${ev.id}"></article>`,
  $: sel => sel === '#feed' ? feed : null,
  Relay: {
    async ready(){ return true; },
    homeAnswered: () => false,
    async query(f){
      if (f[0].ids) { const o = f[0].ids.map(id => notes.get(id)).filter(Boolean); o.complete = true; return o; }
      listReads++;
      const a = answers.shift() || 'answer', o = a === 'answer' ? [NEW] : [];
      o.complete = a === 'answer'; return o;
    },
  },
};
const names = Object.keys(g);
const api = new Function(...names, shipped + `
  ;return { fetchBookmarks, renderBookmarks, setView: v => { VIEW = v; }, ids: () => [...BOOKMARKS] };`)(...names.map(k => g[k]));

await api.fetchBookmarks();                    // the boot read after the reload: missed
api.setView('bookmarks');
await api.renderBookmarks();                   // the person opens Bookmarks
const deadline = Date.now() + 20000;
while (Date.now() < deadline && (feed.innerHTML.match(/<article/g) || []).length < 2)
  await new Promise(r => setTimeout(r, 50));
process.stdout.write(JSON.stringify({
  shown: (feed.innerHTML.match(/<article/g) || []).length, bookmarks: api.ids().sort(), listReads,
}));
process.exit(0);
