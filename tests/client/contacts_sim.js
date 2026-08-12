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
  async enable(){ calls.push(['enable']); return { granted:true, account: !opt.noAccount }; },
  async disable(){ calls.push(['disable']); phoneRows = []; return {}; },
  async begin(a){
    calls.push(['begin', a]);
    if(opt.beginThrows) throw new Error('bridge is gone');
    const hashes = {};
    for(const u of phoneRows) hashes[u] = 'h:' + u;
    // `account:false` is the phone that could not create the PosterChan account. Every row hangs off
    // it, so this is "nothing will ever be written" — and it used to arrive here as a rejected call
    // the client swallowed into silence.
    if(opt.noAccount) return { granted:true, account:false, count:0 };
    return { granted: opt.revoked ? false : true, account:true, hashes, count: phoneRows.length };
  },
  async put(a){
    calls.push(['put', (a.cards || []).map(c => c.uid)]);
    const sent = (a.cards || []).length;
    const before = phoneRows.length;
    /* THE PROVIDER THAT ACCEPTS EVERYTHING AND STORES NOTHING. `putNoop` is the shape this feature
     * actually failed in: applyBatch does not throw for an operation that changes nothing, so a
     * sweep can report success while the phone's Contacts app stays empty. The reply carries the
     * MEASURED row count either way, which is the only thing that can tell the two apart. */
    if(!opt.putNoop){
      // A written card IS on the phone from here on — and the reconcile that follows reads the rows
      // fresh, so a stub that forgot them would be asked to prune a phone that is missing everything
      // the sweep just wrote, i.e. the one shape the guards exist to refuse.
      for(const c of (a.cards || [])) if(!phoneRows.includes(c.uid)) phoneRows.push(c.uid);
    }
    return { written: opt.putNoop ? 0 : sent, sent, held:0, before, after: phoneRows.length,
             account:true, ops: sent * 2, applied: opt.putNoop ? 0 : sent * 2,
             noop: opt.putNoop ? sent * 2 : 0, batches:1, failed:0, error:'' };
  },
  /* commit() WITH THE PLUGIN'S OWN COLLAPSE GUARD, because that guard is the load-bearing one: it is
   * the last thing between a keep-set built from a bad read and somebody's dialer. `nativeGuard:false`
   * runs the phone as it was before it existed, which is how a test can show the difference rather
   * than assert it. See ContactSyncPlugin.commit / isCollapse. */
  async commit(a){
    calls.push(['commit', (a.uids || []).slice(), a.force ? 'force' : '']);
    const keep = new Set(a.uids || []);
    const doomed = phoneRows.filter(u => !keep.has(u));
    const kept = phoneRows.length - doomed.length;
    if(opt.nativeGuard !== false && !a.force && doomed.length > 0 && doomed.length > kept){
      return { refused:true, removed:0, would:doomed.length, kept, count:phoneRows.length };
    }
    phoneRows = phoneRows.filter(u => keep.has(u));
    return { removed:doomed.length, count:phoneRows.length };
  },
  async pull(a){
    calls.push(['pull', a]);
    if(opt.noAccount) return { granted:true, account:false, rows:[] };
    return { granted:true, account:true, rows:(opt.pullRows || []), pushed:{} };
  },
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
      // ONE BOOK OUT OF SEVERAL FAILING is the shape that emptied a phone: the client swallowed it
      // into `[]`, the load still looked complete, and the reconcile was handed a short keep-set.
      if((opt.failBooks || []).includes(book)) throw new Error('NetworkError: failed to fetch');
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
    else if(step === 'ok'){ opt.failLoad = false; opt.failBooks = []; }
    else if(step.slice(0, 10) === 'failbooks:'){ opt.failBooks = step.slice(10).split(',').filter(Boolean); }
    else if(step.slice(0, 7) === 'remove:'){       // somebody deleted a contact in the web UI
      const [book, uid] = step.slice(7).split(':');
      opt.cards[book] = (opt.cards[book] || []).filter(c => c.uid !== uid);
    }
    else if(step === 'fail'){ opt.failLoad = true; }
    else if(step === 'settle'){ await new Promise(r => setTimeout(r, 30)); }
  }
  await new Promise(r => setTimeout(r, 30));      // let any trailing sweep finish
  // `diag` is the line the phone-book panel puts on screen. It exists because this feature was
  // debugged blind across four APK builds and its failure mode reports success — see contacts.js.
  const diag = (C && C.lastSweep) ? C.lastSweep() : '';
  console.log(JSON.stringify({ calls, toasts, fetched, phoneRows, settings, diag }));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
