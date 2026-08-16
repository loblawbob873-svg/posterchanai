/* THE NATIVE TICK: does a backgrounded Android app actually sweep?
 *
 * Reported as "syncing stops every time the screen goes off", with "Stay connected" already on. The
 * cause is three facts that only bite together, and none of them is visible from any one file:
 *
 *   1. fs-android.js has NO watcher — `watch()` answers false and `onChanged()` is empty, because
 *      SAF exposes no usable tree notification. So nothing filesystem-side ever asks for a sweep.
 *   2. That leaves one automatic trigger, a JS `setInterval`, and Android throttles timers in a
 *      hidden WebView into uselessness.
 *   3. `nudge()` additionally refuses while `document.hidden`, unless `_keptAlive` — which is read
 *      from a plugin call that can fail, on the exact platform where it matters.
 *
 * So the clock moved native (StayAwakeService's Handler, which is not throttled) and arrives as
 * `folderSyncTick`. This drives the SHIPPED sync.js in a screen-off world and asserts a sweep really
 * happens — the assertion is that the platform adapter's `scan()` was called, because that is the
 * first thing a real sweep does and it cannot be faked by the trigger alone.
 *
 * Run: node tests/client/sync_tick_sim.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const CLIENT = path.join(__dirname, '..', '..', 'static', 'js', 'client');
const PUBKEY = 'a'.repeat(64);
// sync.js scopes the folder list to the signed-in account (`pc_sync_folders_<pubkey>`). Writing the
// unscoped key looks identical from the outside — an empty folder list, no sweep — which is exactly
// what a broken tick would look like, so this constant is load-bearing for the test to mean anything.
const FOLDERS_KEY = 'pc_sync_folders_' + PUBKEY;

/* A world with the screen OFF: `document.hidden` true, no Electron bridge, and — deliberately — a
 * `stayConnected` read that FAILS. That is the pessimistic case: `_keptAlive` stays false, so
 * anything that re-derives "may I run" from it is refused. The tick must survive that, because its
 * own existence already proves the service is up. */
function boot(opts){
  const o = opts || {};
  const seen = { scans: 0, ticks: 0, statuses: [] };
  const timers = [];

  const pcFs = {
    async scan(){ seen.scans++; return { files: {}, skipped: [] }; },
    async read(){ return new Uint8Array(); },
    async write(){ return { size: 0, mtime: 1 }; },
    async move(){ return true; },
    async trash(){ return '.pc-trash/x'; },
    async power(){ return { charging: true, metered: false, online: true }; },
    watch: async () => false,
    unwatch: async () => false,
    onChanged: () => {},
  };
  // An APK older than the plugin has no onTick at all; that must not throw, it must simply behave
  // the way it does today.
  if(o.withTick !== false){
    pcFs.onTick = (fn) => { pcFs._fire = () => { seen.ticks++; fn(); }; return true; };
  }

  const ctx = {
    console, JSON, Promise, Date, Math, Object, Array, String, Number, Error, Boolean, Map, Set,
    RegExp, TextEncoder, TextDecoder, Buffer,
    setTimeout, clearTimeout, clearInterval,
    // Intervals are captured, never run: the whole point is that the JS heartbeat does NOT fire in
    // a hidden WebView. Letting it run here would test the very thing Android takes away.
    setInterval: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    localStorage: (() => { const m = new Map(); return {
      getItem: k => (m.has(k) ? m.get(k) : null),
      setItem: (k, v) => m.set(k, String(v)),
      removeItem: k => m.delete(k),
    }; })(),
    // A device with no usable IndexedDB. `_loadBase` falls back to localStorage, which is the path
    // an older phone actually takes — and it must carry an `error`, or sync.js's handler reports
    // `undefined` and the rejection surfaces as unrelated noise in this file's output.
    indexedDB: { open(){
      const rq = {};
      setTimeout(() => { rq.error = new Error('no indexeddb here'); if(rq.onerror) rq.onerror(); }, 0);
      return rq;
    } },
    navigator: { onLine: true, userAgent: 'node' },
    document: { hidden: o.hidden !== false, addEventListener(){}, querySelector: () => null,
                getElementById: () => null, querySelectorAll: () => [] },
    fetch: async () => ({ ok: true, status: 200, json: async () => ({ ok: true, manifest: {} }) }),
    btoa: s => Buffer.from(String(s), 'binary').toString('base64'),
  };
  ctx.window = ctx; ctx.globalThis = ctx; ctx.self = ctx;
  // Captured, so a test can raise a REAL `online` the way a reconnecting radio does, instead of
  // reaching into sync.js for a hook that would only exist for the test.
  const listeners = {};
  ctx.addEventListener = (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); };
  ctx.pcFs = pcFs;
  // sync.js reads `window.__PC`, NOT `window.PC` — the same name sync_store_sim.js uses. Getting
  // this wrong is invisible: PC.me() answers undefined, the folder list is keyed on "anon", and
  // folders() returns [] — a sweep that never runs, which is precisely the bug under test.
  ctx.__PC = {
    VIEW: 'home',
    me: () => ({ pubkey: PUBKEY }),
    signAuth: async () => ({ sig: 'x' }),
    enc: s => String(s),
    toast(){},
    uiConfirm: async () => false,
    nip44enc: async (pk, s) => 'sealed:' + s.length,
    nip44dec: async () => '{}',
    syncBlobs: { put: async () => 'sha', get: async () => new Uint8Array() },
    // The read that fails, on purpose — see the note above.
    capPlugin: () => { throw new Error('no such plugin'); },
  };
  ctx.PC = ctx.__PC;
  ctx.PCFolderSync = require(path.join(CLIENT, 'foldersync.js'));
  ctx.PCSyncRun = require(path.join(CLIENT, 'syncrun.js'));
  ctx.ClientSettings = { get: (k, d) => d, set(){} };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(CLIENT, 'sync.js'), 'utf8'), ctx, { filename: 'sync.js' });

  // One folder, started (not paused) and never yet swept, so the policy has no reason of its own to
  // decline: whether a sweep happens is then exactly the question this file is asking.
  ctx.localStorage.setItem(FOLDERS_KEY, JSON.stringify([{
    id: 'content://tree/primary%3APictures', key: 'Pictures', dir: 'Pictures', name: 'Pictures',
    excludes: [], prefs: { paused: false }, lastSyncAt: 0, lastFullScanAt: 0,
  }]));
  return { ctx, pcFs, seen, timers, fire: (ev) => (listeners[ev] || []).forEach(f => f()) };
}

const sleep = ms => new Promise(r => setTimeout(r, ms));

const results = [];
async function check(name, fn){
  try{ await fn(); results.push({ name, ok: true }); }
  catch(e){ results.push({ name, ok: false, why: (e && e.message) || String(e) }); }
}
function assert(cond, msg){ if(!cond) throw new Error(msg); }

(async () => {

  await check('a native tick sweeps a folder with the screen off', async () => {
    const { ctx, pcFs, seen } = boot({ hidden: true });
    ctx.PCSync.startAll();
    await sleep(50);
    const before = seen.scans;
    assert(typeof pcFs._fire === 'function', 'sync.js never subscribed to the native tick');
    pcFs._fire();
    await sleep(2200);            // past nudge()'s 1500ms coalescing window
    assert(seen.scans > before,
      'the tick arrived and no sweep ran — this is the screen-off bug, and _keptAlive is false here '
      + 'exactly as it is on a phone whose stayConnected read failed');
  });

  await check('without the tick, a hidden app sweeps nothing (the bug, reproduced)', async () => {
    const { ctx, seen, timers } = boot({ hidden: true, withTick: false });
    ctx.PCSync.startAll();
    await sleep(50);
    const before = seen.scans;
    // Everything a backgrounded phone actually has: the JS heartbeat. Run it by hand — in a real
    // hidden WebView it is throttled and mostly does not fire at all, so this is the GENEROUS case.
    for(const t of timers) t.fn();
    await sleep(2200);
    assert(seen.scans === before,
      'a hidden app with no native tick swept anyway — then this simulation is not reproducing the '
      + 'reported state, and the test above proves nothing');
  });

  await check('an APK with no onTick still starts, and does not throw', async () => {
    const { ctx } = boot({ hidden: true, withTick: false });
    ctx.PCSync.startAll();               // must not throw on an older bridge
    await sleep(50);
  });

  await check('an unforced nudge arriving right after a tick cannot cancel it', async () => {
    const { ctx, pcFs, seen, fire } = boot({ hidden: true });
    ctx.PCSync.startAll();
    await sleep(50);
    const before = seen.scans;
    /* THE CORRELATED CASE, not a rare race. The phone wakes, the pending alarm fires a FORCED nudge,
     * and milliseconds later the reconnecting radio raises `online` — which coalesces into the same
     * single timer. With the flag held per-CALL instead of per-pending-nudge, the unforced one wins,
     * `_idle()` is true (screen off, stayConnected read threw), and the sweep is skipped for another
     * whole alarm period. */
    pcFs._fire();
    await sleep(100);
    fire('online');               // the real listener sync.js registered, not a back door
    await sleep(2200);
    assert(seen.scans > before,
      'an unforced nudge swallowed the forced one — the tick fires and the sweep never happens');
  });

  await check('a request that never answers does not strand the folder for ever', async () => {
    /* THE ONE THAT NEEDED THE APP FORCE-CLOSED.
     *
     * `fetch` imposes no timeout, and nothing in the sync path had one. A socket that dies without
     * an RST — a phone leaving the house, Wi-Fi handing over to cellular — leaves the request
     * pending indefinitely: it neither resolves nor rejects. The sweep stops on that await, `running`
     * is cleared only in a `finally` that never runs, and every later press of Sync silently returns
     * the dead promise. Pause cannot help either: it is checked BETWEEN files.
     *
     * Reported as "left the house and came back, stuck, no progress; Pause and Sync now says already
     * syncing but no file transfer" — and then "had to force close and reopen the app", which is the
     * only exit an unbounded await leaves. It also explains the background sync stopping after a
     * period and never resuming, which no amount of wake lock could fix.
     *
     * The assertion is that the sweep ENDS. Not that it succeeds — a dead network cannot be made to
     * work — but that it fails, so the folder is usable again without killing the process. */
    const { ctx, seen } = boot({ hidden: false });
    ctx.fetch = () => new Promise(() => {});          // never resolves, never rejects
    ctx.PCSync.startAll();
    await sleep(50);
    const f = ctx.PCSync.folders()[0];
    const started = Date.now();
    /* SIXTY SECONDS, against a forty-five second ceiling. The first version of this raced at eight
     * and reported HUNG against a working timeout — proving only that the test was impatient. The
     * point is that the ceiling EXPIRES, so the wait has to outlast it; a slow check is the honest
     * price of testing a timeout rather than asserting one exists. */
    const rep = await Promise.race([
      ctx.PCSync.sweep(f, { manual: true }).catch(e => ({ error: (e && e.message) || String(e) })),
      sleep(60000).then(() => 'HUNG'),
    ]);
    assert(rep !== 'HUNG',
      'the sweep never returned — the folder is stranded until the app is force-closed, which is '
      + 'exactly what was reported');
    // …and the folder must be usable again, not stuck reporting "already syncing".
    const second = await Promise.race([
      ctx.PCSync.sweep(f, { manual: true }).catch(() => 'failed-again'),
      sleep(60000).then(() => 'HUNG'),
    ]);
    assert(second !== 'HUNG', 'a second sweep hung too, so `running` was never cleared');
    void seen; void started;
  });

  await check('the tick does not bypass the battery and network policy', async () => {
    const { ctx, pcFs, seen } = boot({ hidden: true });
    // What "only when plugged in" and "Wi-Fi only" mean, on a phone that is on neither.
    pcFs.power = async () => ({ charging: false, metered: true, online: true });
    ctx.PCSync.startAll();
    await sleep(50);
    const before = seen.scans;
    pcFs._fire();
    await sleep(2200);
    assert(seen.scans === before,
      'a native tick swept on metered data — the tick must skip the "is anyone looking" test and '
      + 'nothing else; shouldSync still decides');
  });

  await check('a paused folder is still not started by a tick', async () => {
    const { ctx, pcFs, seen } = boot({ hidden: true });
    const l = JSON.parse(ctx.localStorage.getItem(FOLDERS_KEY));
    l[0].prefs.paused = true;
    ctx.localStorage.setItem(FOLDERS_KEY, JSON.stringify(l));
    ctx.PCSync.startAll();
    await sleep(50);
    const before = seen.scans;
    pcFs._fire();
    await sleep(2200);
    assert(seen.scans === before,
      'a folder the user has never pressed Start on was swept by a background tick');
  });

  /* JSON on stdout, like sync_store_sim.js, so the unittest wrapper can name each scenario as its
   * own test rather than reporting one opaque pass/fail. sync.js's own console.warn goes to stderr,
   * which is why stdout stays parseable. */
  process.stdout.write(JSON.stringify(results.map(r => ({
    name: r.name, ok: r.ok, detail: r.ok ? null : r.why,
  }))));
  process.exit(0);
})();
