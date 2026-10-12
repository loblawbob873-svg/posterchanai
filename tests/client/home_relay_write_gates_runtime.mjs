/* Every write gate on one of the account's OWN private documents, run from the SHIPPED source under
 * node against a relay pool in two worlds:
 *
 *   home    this node's relay EOSE'd and another relay in the pool never answered -- the pool of a
 *           member with "use my own relays" on (measured: filter.nostr.wine's AUTH hangs, rss.nos.social
 *           is silent). `complete` is FALSE, `answeredBy` is [home]. The document is not there yet.
 *           The write MUST go out: the node's relay holds every one of these documents.
 *   nobody  no relay answered at all (`complete:false`, `answeredBy:[]`). Nothing may be written: a
 *           replaceable document published over a read nobody answered replaces the real one.
 *
 * `homeAnswered` is relay.js's own method, lifted from the file -- never a copy of its rule.
 * Driven by tests/client/test_private_doc_write_gates_hear_the_home_relay.py, which DISCOVERS the
 * modules that write such a document and fails for any that is neither driven here nor marked
 * `HOME-RELAY WRITE GATE: none -- <reason>` in its own source.
 *
 * Prints one JSON object: { "<module>.js": { "<gate>": { home, nobody } } }, where home / nobody are
 * what the gate DID (true = wrote / may write) -- or { wired } for a caller that must hand a
 * dependency-injected module the predicate (seen.js, livetemplates.js take it as an option).
 */
import fs from 'node:fs';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { clientSourceAt } from './client_source.mjs';

// PC_CLIENT_DIR runs the same gates against another copy of the client (the fail-first run).
const CLIENT = process.env.PC_CLIENT_DIR ? pathToFileURL(process.env.PC_CLIENT_DIR.replace(/\/?$/, '/'))
                                         : new URL('../../static/js/client/', import.meta.url);
const clientSource = () => clientSourceAt(new URL('app.js', CLIENT));
const src = name => fs.readFileSync(new URL(name, CLIENT), 'utf8');
const HOME = 'wss://home.example', OTHER = 'wss://silent.example';
const ME = 'a'.repeat(64);
const tick = (ms = 0) => new Promise(r => setTimeout(r, ms));
const settle = async (n = 40) => { for(let i = 0; i < n; i++) await tick(5); };

/* The text of one declaration, `decl` through its matching brace. Skips strings, template literals,
 * comments and regex-free code well enough for the functions lifted here; throws if the anchor moved,
 * so a rename fails loudly instead of testing nothing. */
function lift(source, decl){
  const start = source.indexOf(decl);
  if(start < 0) throw new Error('moved or renamed: ' + decl);
  let i = source.indexOf('{', start + decl.length - 1), depth = 0;
  for(; i < source.length; i++){
    const c = source[i], n = source[i + 1];
    if(c === '/' && n === '/'){ i = source.indexOf('\n', i); continue; }
    if(c === '/' && n === '*'){ i = source.indexOf('*/', i + 2) + 1; continue; }
    if(c === '"' || c === "'" || c === '`'){
      for(i++; i < source.length && source[i] !== c; i++) if(source[i] === '\\') i++;
      continue;
    }
    if(c === '{') depth++;
    else if(c === '}' && --depth === 0) return source.slice(start, i + 1);
  }
  throw new Error('unbalanced: ' + decl);
}

/* `lift`, or '' when the declaration does not exist -- for a HELPER a gate may or may not use, so the
 * fail-first run against an older client measures that client's behaviour instead of a missing name. */
const liftOpt = (source, decl) => (source.indexOf(decl) < 0 ? '' : lift(source, decl));

/* relay.js's homeAnswered, the method itself. */
const HOME_ANSWERED = (() => {
  const text = lift(src('relay.js'), 'homeAnswered(got){');
  return vm.runInNewContext('({' + text + '})').homeAnswered;
})();

function answer(events, world){
  const a = [...events];
  Object.defineProperty(a, 'complete', { value: false, enumerable: false });
  Object.defineProperty(a, 'answeredBy', { value: Object.freeze(world === 'home' ? [HOME] : []), enumerable: false });
  return a;
}
/* A pool whose home relay answered (or did not), holding nothing for this account. */
function pool(world, events = () => []){
  const R = { _home: HOME, status: 'ok', url: HOME, urls: () => [HOME, OTHER],
              ready: async () => true, query: async (filters) => answer(events(filters), world),
              subscribe: () => 'sub', close(){}, publish: async () => ({ ok: true }) };
  R.homeAnswered = HOME_ANSWERED.bind(R);
  return R;
}
const storage = () => { const m = new Map();
  return { getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: k => m.delete(k) }; };
const baseCtx = () => ({ console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval(){}, Promise, JSON, Math,
  Date, Object, Array, String, Number, Map, Set, Error, RegExp, Uint8Array, ArrayBuffer, TextEncoder, TextDecoder,
  crypto: webcrypto, atob, btoa, URL, localStorage: storage(), navigator: { onLine: true },
  document: { addEventListener(){}, querySelector: () => null, querySelectorAll: () => [], getElementById: () => null,
              body: { classList: { contains: () => false, add(){}, remove(){}, toggle(){} }, appendChild(){} },
              head: { appendChild(){} }, documentElement: {}, createElement: () => ({ style: {}, classList: { add(){} } }),
              visibilityState: 'visible' },
  addEventListener(){}, removeEventListener(){}, getComputedStyle: () => ({ zoom: '1' }) });

/* Run `fn(world)` in both worlds; it answers true when the gate WROTE (or allowed a write). */
async function both(fn){
  const out = {};
  for(const world of ['home', 'nobody']){
    try{ out[world] = !!(await fn(world)); }
    catch(e){ out[world] = false; out[world + 'Error'] = String(e && e.message || e).slice(0, 200); }
  }
  return out;
}

const result = {};
const put = (mod, gate, r) => { (result[mod] = result[mod] || {})[gate] = r; };

/* ---- blossom.js: the synced client prefs (pcai:client-prefs). `_readPrefs` answers {} ("no document
 * yet, a first save is safe") or null ("could not ask"); the save publishes only on the former. */
put('blossom.js', 'client prefs read', await both(async world => {
  const s = src('blossom.js');
  const code = liftOpt(s, 'const _homeAnswered = got => {') + ';\n' + lift(s, 'async function _readPrefs(') +
               '\nthis.run = _readPrefs;';
  const ctx = Object.assign(baseCtx(), { _S: { ME: { pubkey: ME } } });
  ctx.window = ctx; ctx.Relay = pool(world);
  vm.createContext(ctx); vm.runInContext(code, ctx);
  const got = await ctx.run(ME);
  return got !== null;
}));

/* ---- ai.js: the saved agent tasks (pcai:agent-tasks -- a device holding tasks from before the doc
 * existed publishes them on the first read that finds none) and the saved voices (pcai:voices --
 * `voicesRead` answers [] "an empty library, a first save may start" or null "could not ask"). */
put('ai.js', 'agent tasks first save', await both(async world => {
  const s = src('ai.js');
  const code = ["const _AGT_D = 'pcai:agent-tasks'; let _agtDoc = null, _agtChain = Promise.resolve(), _agtLoading = null;",
    liftOpt(s, 'function _aiAnswered(evs){'), lift(s, 'function _agentSavedGet(){'), lift(s, 'async function _agentSavedRead(){'),
    lift(s, 'function _agentSavedPublish(){'), 'this.run = _agentSavedRead;'].join('\n');
  const ctx = baseCtx(); ctx.window = ctx; ctx.Relay = pool(world);
  let publishes = 0;
  Object.assign(ctx, { S: { ME: { pubkey: ME }, signer: { nip44enc: async () => 'ct', nip44dec: async () => '[]' } },
    ClientSettings: { get: () => [{ name: 'task saved on this device' }], set(){} }, toast(){},
    publish: async () => { publishes++; return { ok: true }; } });
  vm.createContext(ctx); vm.runInContext(code, ctx);
  await ctx.run(); await settle(4);
  return publishes > 0;
}));
put('ai.js', 'voices read', await both(async world => {
  const s = src('ai.js');
  const code = ["const VOICES_D = 'pcai:voices';", liftOpt(s, 'function _aiAnswered(evs){'),
    lift(s, 'async function voicesRead(){'), 'this.run = voicesRead;'].join('\n');
  const ctx = baseCtx(); ctx.window = ctx; ctx.Relay = pool(world); ctx.S = { ME: { pubkey: ME } };
  vm.createContext(ctx); vm.runInContext(code, ctx);
  return (await ctx.run()) !== null;
}));

/* ---- budget.js (pcai:budget): the whole module, through its public entry. */
put('budget.js', 'budget save', await both(async world => {
  const ctx = baseCtx(); ctx.window = ctx;
  let publishes = 0;
  ctx.Relay = pool(world);
  ctx.__PC = { VIEW: 'timeline', ME: { pubkey: ME }, $: () => null, enc: String, toast(){}, async uiConfirm(){ return true; },
    modal(){}, closeModal(){}, isView: () => false, async nip44dec(){ return '{}'; }, async nip44enc(){ return 'ct'; },
    async publish(){ publishes++; return { ok: true }; } };
  vm.createContext(ctx); vm.runInContext(src('budget.js'), ctx);
  await ctx.PCBudget.addParsed('rent', 10);
  return publishes > 0;
}));

/* ---- concord.js: the community rail folders (pcai:concord-rail) and read marks (pcai:concord-read).
 * The fetchers answer null for "could not ask" -- and the savers write nothing on null. */
for(const [gate, fn, d] of [['rail folders read', 'fetchRailDoc', 'pcai:concord-rail'],
                            ['read marks read', 'fetchReadDoc', 'pcai:concord-read']]){
  put('concord.js', gate, await both(async world => {
    const s = src('concord.js');
    const code = 'const RAIL_D="pcai:concord-rail", READ_D="pcai:concord-read";\n' + liftOpt(s, 'function docAnswered(evs){') +
                 '\n' + lift(s, 'async function ' + fn + '(') + '\nthis.run = ' + fn + ';';
    const ctx = baseCtx(); ctx.window = ctx; ctx.Relay = pool(world); ctx.Store = { query: () => [] };
    vm.createContext(ctx); vm.runInContext(code, ctx);
    const p = { relayQuery: (f, t) => ctx.Relay.query(f, t), nip44dec: async () => '{}' };
    const got = fn === 'fetchRailDoc' ? await ctx.run(p, ME, d) : await ctx.run(p, ME);
    return got !== null;
  }));
}

/* ---- dms.js: the DM cache key (pcai:dmkey, minted once and published) and the shared-cache pointer
 * (pcai:dmcache, which must never be replaced by a smaller cache after a read nobody answered). */
function dmCache(world, published){
  const s = src('dms.js');
  const pub = liftOpt(s, 'async _published(){');
  const obj = '({ D:"pcai:dmkey", SHARE_D:"pcai:dmcache", ' + lift(s, 'async _loadKey(){') + (pub ? ', ' + pub : '') + ' })';
  const code = liftOpt(s, 'function _dmAnswered(evs){') + '\nthis.cache = ' + obj + ';';
  const ctx = baseCtx(); ctx.window = ctx; ctx.Relay = pool(world); ctx.Store = { query: () => [] };
  ctx.S = { ME: { pubkey: ME }, signer: { nip44dec: async () => '', nip44enc: async () => 'ct' } };
  ctx.publish = async (...a) => { published.push(a); return { ok: true }; };
  vm.createContext(ctx); vm.runInContext(code, ctx);
  return ctx.cache;
}
put('dms.js', 'DM cache key', await both(async world => {
  const published = [];
  await dmCache(world, published)._loadKey();
  return published.some(a => JSON.stringify(a[2]) === JSON.stringify([['d', 'pcai:dmkey']]));
}));
put('dms.js', 'shared cache pointer read', await both(async world => (await dmCache(world, [])._published()) !== null));

/* ---- files.js: the DM cache pointer as the reclaim scan reads it ('' = "no cache", null = "could not
 * ask", which withholds the reclaim offer). */
put('files.js', 'dm cache reference', await both(async world => {
  const code = lift(src('files.js'), 'async function _dmCacheRef(){') + '\nthis.run = _dmCacheRef;';
  const ctx = baseCtx(); ctx.window = ctx; ctx.Relay = pool(world); ctx._S = { ME: { pubkey: ME } };
  vm.createContext(ctx); vm.runInContext(code, ctx);
  return (await ctx.run()) !== null;
}));

/* ---- livetemplates.js (pcai:livetemplates), wired exactly as streams.js wires it. */
put('livetemplates.js', 'go-live templates save', await both(async world => {
  const ctx = baseCtx(); ctx.window = ctx; ctx.module = undefined;
  vm.createContext(ctx); vm.runInContext(src('livetemplates.js'), ctx);
  const R = pool(world); let publishes = 0, local = [];
  const st = ctx.PCLiveTpl.store({ owner: ME, query: f => R.query(f), homeAnswered: got => !!R.homeAnswered(got),
    publish: async () => { publishes++; return { ok: true }; }, enc: async () => 'ct', dec: async () => '{}',
    local: { get: () => local, set: l => { local = l; } } });
  await st.save(l => ctx.PCLiveTpl.upsert(l, { name: 'Weekly show', title: 'Weekly show' }));
  return publishes > 0;
}));

/* …and streams.js, which builds that store, must hand it the predicate, or the module's rule never runs. */
put('streams.js', 'go-live templates wiring',
    { wired: /homeAnswered\s*:/.test(lift(src('streams.js'), 'window.PCLiveTpl.store({')) });

/* ---- musicshare.js: the accept/reject answers (pcai:musicshares) -- the whole module. */
put('musicshare.js', 'share decisions save', await both(async world => {
  const ctx = baseCtx(); ctx.window = ctx;
  const published = [];
  ctx.Relay = pool(world); ctx.Store = { query: () => [], saveEvent(){} };
  ctx.__PC = { me: () => ({ pubkey: ME }), nip44enc: async () => 'ct', nip44dec: async () => '{}', toast(){}, enc: String,
    publish: async (kind, content, tags) => { published.push(tags); return { ok: true }; } };
  vm.createContext(ctx); vm.runInContext(src('musicshare.js'), ctx);
  ctx.PCMusicShare.decide('b'.repeat(64) + ':x', true);
  await settle();
  return published.some(t => JSON.stringify(t) === JSON.stringify([['d', 'pcai:musicshares']]));
}));
put('musicshare.js', 'my shares read', await both(async world => {
  const ctx = baseCtx(); ctx.window = ctx;
  ctx.Relay = pool(world); ctx.Store = { query: () => [], saveEvent(){} };
  ctx.__PC = { me: () => ({ pubkey: ME }), nip44enc: async () => 'ct', nip44dec: async () => '{}', toast(){}, enc: String,
    publish: async () => ({ ok: true }) };
  vm.createContext(ctx); vm.runInContext(src('musicshare.js'), ctx);
  // null = "my shares could not be read" -- which also refuses every share, release and re-send
  return (await ctx.PCMusicShare.refIds()) !== null;
}));

/* ---- os.js: the desktop layout (pcai:desktop). */
put('os.js', 'desktop layout', await both(async world => {
  const ctx = baseCtx(); ctx.window = ctx; ctx.global = ctx;
  ctx.Relay = pool(world); ctx.Store = { query: () => [] };
  ctx.__PC = { me: () => ({ pubkey: ME }), nip44dec: async () => '{}', nip44enc: async () => 'ct' };
  vm.createContext(ctx); vm.runInContext(src('os.js'), ctx);
  await ctx.PCOS.__loadLayout();
  return ctx.PCOS.__layoutWritable();
}));

/* ---- seen.js (pcai:seen), with the option app.js passes. */
put('seen.js', 'read markers save', await both(async world => {
  const ctx = baseCtx(); vm.createContext(ctx); vm.runInContext(src('seen.js'), ctx);
  const R = pool(world), mem = new Map(); let publishes = 0;
  const e = ctx.PCSeen.create({ owner: ME, delay: 0, retry: 60000,
    storage: { get: k => mem.get(k) || null, set: (k, v) => mem.set(k, v) },
    query: f => R.query(f), homeAnswered: got => !!R.homeAnswered(got), cached: () => [],
    decrypt: async () => '{}', encrypt: async t => t,
    publish: async () => { publishes++; return { ok: true }; }, isCurrent: () => true });
  e.markNotif(Math.floor(Date.now() / 1000) - 5);
  await settle();
  await e.flush();
  e.stop();
  return publishes > 0;
}));

put('app.js', 'read markers wiring', { wired: /homeAnswered\s*:/.test(lift(clientSource(), 'PCSeen.create({')) });

/* ---- vault.js: the vault KEY (pcai:pwkey -- minted once, only after a relay answered there is none)
 * and the Android autofill snapshot (written only after a read that answered). */
async function vaultCtx(world, { cachedKey = false } = {}){
  const ctx = baseCtx(); ctx.window = ctx;
  vm.createContext(ctx); vm.runInContext(src('vaultcore.js'), ctx);
  const V = ctx.PCVaultCore;
  if(cachedKey) ctx.localStorage.setItem('pcaiVaultKey:raw:' + ME, V.toB64(V.newVaultKey()));
  const published = [], android = [];
  ctx.Relay = pool(world); ctx.Store = { query: () => [], saveEvent(){} };
  ctx.__PC = { ME: { pubkey: ME, mode: 'local' }, me: () => ({ pubkey: ME }), VIEW: 'home', isView: () => false,
    nip44enc: async () => 'wrapped', nip44dec: async () => '{}',
    publish: async (kind, content, tags) => { published.push(tags); return { ok: true }; } };
  ctx.Capacitor = { Plugins: { VaultAutofill: { put: async ({ items }) => { android.push(items); return { ok: true }; }, clear: async () => {} } } };
  vm.runInContext(src('vault.js'), ctx);
  return { ctx, published, android };
}
put('vault.js', 'vault key', await both(async world => {
  const v = await vaultCtx(world);
  await v.ctx.PCVault.__ensureKey(true);
  return v.published.some(t => t.some(x => x[0] === 'd' && x[1] === 'pcai:pwkey'));
}));
put('vault.js', 'autofill snapshot', await both(async world => {
  const v = await vaultCtx(world, { cachedKey: true });
  await v.ctx.PCVault.syncBackground();
  return v.android.length > 0;
}));

/* ---- vms.js: the VM host list (pcai:vmhosts) -- the whole module, adding a host the way a person does. */
put('vms.js', 'host list save', await both(async world => {
  const R = pool(world);
  const feed = { innerHTML: '', getBoundingClientRect: () => ({ width: 390 }), querySelector: () => null, querySelectorAll: () => [] };
  const ctx = baseCtx(); ctx.window = ctx; ctx.innerWidth = 390;
  ctx.document.querySelector = s => (s === '#feed' ? feed : null);
  ctx.Relay = R;
  ctx.PCVmRpc = { createVmRpc: () => ({ call: () => new Promise(() => {}), closeAll(){} }) };
  const published = [], answers = ['ef'.repeat(32), 'wss://new.example/relay'];
  ctx.__PC = { me: () => ({ pubkey: ME }), isView: v => v === 'vms', toast(){}, clientConfig: () => ({}),
    relayQuery: (f, t) => R.query(f, t), nip44dec: async () => '{}', nip44enc: async () => 'ct',
    publish: async (...a) => { published.push(a); return { ok: true }; },
    uiPrompt: async () => answers.shift(), uiConfirm: async () => false };
  vm.createContext(ctx); vm.runInContext(src('vms.js'), ctx);
  ctx.PCVms.render(); await settle(4);
  await ctx.PCVms._addHost(); await settle(4);
  return published.length > 0;
}));

/* ---- app.js: the automatic-mute switch (pcai:automute). */
put('app.js', 'auto-mute switch save', await both(async world => {
  const app = clientSource();
  const code = ['let _autoMuteDocRead = false, _autoMuteDocPk = "", _autoMuteDocLoading = null;',
    'const AUTO_MUTE_D = "pcai:automute";',
    lift(app, 'function _autoMuteDocReady(){'), lift(app, 'async function _autoMuteDocLoad(){'),
    lift(app, 'async function _autoMuteDocSave(){'),
    'this.load = _autoMuteDocLoad; this.save = _autoMuteDocSave;'].join('\n');
  const ctx = baseCtx(); ctx.window = ctx;
  let publishes = 0;
  Object.assign(ctx, { ME: { pubkey: ME }, GUEST: false, Relay: pool(world),
    signer: { nip44dec: async () => '{}', nip44enc: async () => 'ct' },
    publish: async () => { publishes++; return { ok: true }; },
    _autoMuteStored: () => ({ owner: ME, enabled: true, ignored: [] }), _cacheAutoMute(){}, _paintAutoMuteControls(){},
    _applyAutoMuteToView(){}, _scheduleAutoMutes(){} });
  vm.createContext(ctx); vm.runInContext(code, ctx);
  await ctx.load();
  await ctx.save();
  return publishes > 0;
}));

// Exit once the answer is out: a gate that refused leaves its own retry timers armed (seen.js retries a
// minute later, for one), and those are the module working as designed, not something to wait for.
process.stdout.write(JSON.stringify(result), () => process.exit(0));
