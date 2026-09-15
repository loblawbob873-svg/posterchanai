/* Isolated renderer fixture: full shipped sync modules, controlled platform adapters. */
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
  const seen = { scans: 0, writes: 0, requests: [], ticks: 0, statuses: [] };
  const timers = [];

  const pcFs = {
    async scan(...args){ seen.scans++; return o.scan ? o.scan(...args) : { files: {}, skipped: [] }; },
    async read(){ return new Uint8Array(); },
    async write(){ seen.writes++; return { size: 0, mtime: 1 }; },
    async move(){ seen.writes++; return true; },
    async trash(){ seen.writes++; return '.pc-trash/x'; },
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
    fetch: async (url, options) => { seen.requests.push({url, options}); return { ok: true, status: 200, json: async () =>
      String(url).indexOf('sync-state') !== -1
        ? { ok: true, era: 0, now: 1, full: true, records: [], results: [], folders: [] }
        : { ok: true, manifest: {} } }; },
    btoa: s => Buffer.from(String(s), 'binary').toString('base64'),
  };
  if(o.clock){
    for(const key of ["setTimeout", "clearTimeout", "setInterval", "clearInterval"]) ctx[key] = o.clock[key];
    ctx.Date = class extends Date { static now(){ return o.clock.now(); } };
  }
  ctx.window = ctx; ctx.globalThis = ctx; ctx.self = ctx;
  if(o.storage) ctx.localStorage = o.storage;
  if(o.indexedDB) ctx.indexedDB = o.indexedDB;
  if(o.shell) ctx.pcShell = o.shell;
  if(o.BroadcastChannel) ctx.BroadcastChannel = o.BroadcastChannel;
  if(o.location) ctx.location = o.location;
  ctx.opener = null;
  // Captured, so a test can raise a REAL `online` the way a reconnecting radio does, instead of
  // reaching into sync.js for a hook that would only exist for the test.
  const listeners = {};
  ctx.addEventListener = (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); };
  ctx.removeEventListener = (ev, fn) => { listeners[ev] = (listeners[ev] || []).filter(f => f !== fn); };
  ctx.pcFs = pcFs;
  // sync.js reads `window.__PC`, NOT `window.PC` — the same name sync_store_sim.js uses. Getting
  // this wrong is invisible: PC.me() answers undefined, the folder list is keyed on "anon", and
  // folders() returns [] — a sweep that never runs, which is precisely the bug under test.
  ctx.__PC = {
    VIEW: 'home',
    me: () => ({ pubkey: o.pubkey || PUBKEY }),
    signAuth: async () => ({ sig: 'x' }),
    enc: s => String(s),
    toast(){},
    uiConfirm: o.uiConfirm || (async () => false),
    nip44enc: async (pk, s) => 'sealed:' + s.length,
    nip44dec: async () => '{}',
    syncBlobs: { put: async () => 'sha', get: async () => new Uint8Array() },
    // The read that fails, on purpose — see the note above.
    capPlugin: () => { throw new Error('no such plugin'); },
  };
  ctx.PC = ctx.__PC;
  ctx.PCFolderSync = require(path.join(CLIENT, 'foldersync.js'));
  ctx.PCSyncRun = require(path.join(CLIENT, 'syncrun.js'));
  ctx.PCSyncState = require(path.join(CLIENT, 'syncstate.js'));
  ctx.PCSyncExec = require(path.join(CLIENT, 'syncexec.js'));
  if(o.executor) ctx.PCSyncExec = Object.assign({}, ctx.PCSyncExec, o.executor);
  ctx.ClientSettings = { get: (k, d) => d, set(){} };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(o.source || path.join(CLIENT, 'sync.js'), 'utf8'), ctx, { filename: 'sync.js' });

  // One folder, started (not paused) and never yet swept, so the policy has no reason of its own to
  // decline: whether a sweep happens is then exactly the question this file is asking.
  ctx.localStorage.setItem('pc_sync_folders_' + (o.pubkey || PUBKEY), JSON.stringify([{
    id: 'content://tree/primary%3APictures', key: 'Pictures', dir: 'Pictures', name: 'Pictures',
    excludes: [], prefs: { paused: false }, lastSyncAt: 0, lastFullScanAt: 0,
  }]));
  return { ctx, pcFs, seen, timers, fire: (ev) => (listeners[ev] || []).forEach(f => f()) };
}


module.exports = { boot };
