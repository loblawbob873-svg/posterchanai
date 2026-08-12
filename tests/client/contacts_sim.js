/* Run the SHIPPED static/js/client/contacts.js under node against a stub phone.
 *
 * Why a simulator and not a grep: the bug this exists for (a load that fails still runs the
 * phone-book reconcile, and `commit({uids:[]})` deletes every contact on the handset) is a
 * relationship between two functions, not a string. Only driving the real file can show it.
 *
 * There is no DOM: `paint()` returns immediately unless the contacts view is on screen, and nothing
 * else here touches `document`, so the view is left as something else and every code path under
 * test is the real one.
 *
 * Usage:  node contacts_sim.js '<json options>'   → prints a JSON transcript on stdout.
 */
const path = require('path');
const ROOT = path.resolve(__dirname, '..', '..');

const opt = JSON.parse(process.argv[2] || '{}');

const calls = [];                       // every bridge call, in order
const toasts = [];
const fetched = [];

/* ---- the stub phone ------------------------------------------------------------------------- */
let phoneRows = (opt.phone || []).slice();          // uids ContactsContract holds for our account

const PLUGIN = {
  async status(){ return { granted:true, account:true, owner:opt.owner || 'me', count:phoneRows.length }; },
  async enable(){ calls.push(['enable']); return { granted:true }; },
  async disable(){ calls.push(['disable']); phoneRows = []; return {}; },
  async begin(a){
    calls.push(['begin', a]);
    if(opt.beginThrows) throw new Error('bridge is gone');
    const hashes = {};
    for(const u of phoneRows) hashes[u] = 'h:' + u;
    return { granted: opt.revoked ? false : true, hashes, count: phoneRows.length };
  },
  async put(a){ calls.push(['put', (a.cards || []).map(c => c.uid)]); return { written:(a.cards||[]).length }; },
  async commit(a){
    calls.push(['commit', (a.uids || []).slice()]);
    const keep = new Set(a.uids || []);
    phoneRows = phoneRows.filter(u => keep.has(u));
    return { removed:0, count:phoneRows.length };
  },
  async pull(a){ calls.push(['pull', a]); return { granted:true, rows:(opt.pullRows || []), pushed:{} }; },
  async taken(a){ calls.push(['taken', a]); return {}; },
};

/* ---- the stub client ------------------------------------------------------------------------ */
global.window = global;
global.document = { createElement(){ return { innerHTML:'', firstElementChild:null }; } };
global.requestAnimationFrame = (fn) => setTimeout(fn, 0);

require(path.join(ROOT, 'static', 'js', 'client', 'vcard.js'));   // sets globalThis.PCVcard

const settings = Object.assign({}, opt.settings || {});
global.ClientSettings = {
  get(k, d){ return (k in settings) ? settings[k] : d; },
  set(k, v){ settings[k] = v; },
};

/* A load that fails: exactly what happens when the app opens before wifi associates. */
function makeFetch(){
  return async (url) => {
    fetched.push(url);
    if(opt.failLoad) throw new Error('NetworkError: failed to fetch');
    if(/\/books/.test(url)) return { ok:true, status:200, json:async()=>({ books: opt.books || [] }) };
    if(/\/cards/.test(url)){
      const book = decodeURIComponent((url.match(/book=([^&]*)/) || [,''])[1]);
      return { ok:true, status:200, json:async()=>({ cards:(opt.cards || {})[book] || [] }) };
    }
    return { ok:true, status:200, json:async()=>({}) };
  };
}

global.__PC = {
  VIEW: 'timeline',                     // NOT contacts, so paint() is a no-op and needs no DOM
  $: () => null, $$: () => [],
  enc: (s) => String(s == null ? '' : s),
  toast: (m) => { toasts.push(String(m)); },
  modal(){}, closeModal(){},
  uiConfirm: async () => true,
  ensureAiSession: async () => {},
  authFetch: makeFetch(),
  me: () => ({ pubkey: opt.owner || 'me' }),
  capPlugin: (name, method) => (name === 'ContactSync' && PLUGIN[method || 'begin']) ? PLUGIN : null,
};

require(path.join(ROOT, 'static', 'js', 'client', 'contacts.js'));

(async () => {
  const C = global.PCContacts;
  for(const step of (opt.steps || ['reload'])){
    if(step === 'reload'){ await C.reload(); }
    else if(step === 'syncTick'){ await C.syncTick(); }
    else if(step === 'forget'){ await C.forgetDevice(); }
    else if(step === 'render'){ C.render(); await new Promise(r => setTimeout(r, 20)); }
    else if(step === 'ok'){ opt.failLoad = false; }
    else if(step === 'fail'){ opt.failLoad = true; }
    else if(step === 'settle'){ await new Promise(r => setTimeout(r, 30)); }
  }
  await new Promise(r => setTimeout(r, 30));      // let any trailing sweep finish
  console.log(JSON.stringify({ calls, toasts, fetched, phoneRows, settings }));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
