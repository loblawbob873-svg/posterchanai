// The shipped extension background, run with a fake `browser` whose storage.managed is what PosterChanOS writes.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import {webcrypto} from 'node:crypto';
import { installStateGlobals } from './client/client_source.mjs';

const dir = path.resolve(process.argv[2]), scenario = process.argv[3];
const noop = () => {}, evt = () => ({ addListener: noop, removeListener: noop });
const store = {};
let managed = null;                       // what ~/.mozilla/managed-storage/passwords@poster.place.json holds
let alarm = null;
const code = (pubkey, keyByte, mode = 'full') => Buffer.from(JSON.stringify({ v: 1, t: 'pcvault', pubkey,
  key: Buffer.alloc(32, keyByte).toString('base64'), relay: 'wss://relay.test', relays: ['wss://relay.test'], mode,
  sk: mode === 'full' ? 'aa'.repeat(32) : undefined, api: 'https://node.test' })).toString('base64');
const A = 'a1'.repeat(32), Bk = 'b2'.repeat(32);
const B = { runtime: { onMessage: { addListener: noop }, onInstalled: evt(), onStartup: evt(), onConnect: evt(), onSuspend: evt(),
    getURL: p => 'moz-extension://test/' + p, id: 'passwords@poster.place', getManifest: () => ({ version: 'test' }), sendMessage: async () => ({}) },
  storage: { local: { get: async key => typeof key === 'string' ? { [key]: store[key] } : { ...store },
                      set: async o => Object.assign(store, o), remove: async () => {}, clear: async () => { for (const k in store) delete store[k]; } },
             managed: { get: async () => { if (managed === null) throw new Error('Managed storage manifest not found'); return { pairing: managed }; } },
             session: { get: async () => ({}), set: async () => {} }, onChanged: evt() },
  alarms: { create: noop, onAlarm: { addListener: f => { alarm = f; } } },
  tabs: { query: async () => [], sendMessage: async () => {}, onUpdated: evt(), onRemoved: evt() },
  action: { setBadgeText: noop, setBadgeBackgroundColor: noop, setTitle: noop, onClicked: evt() },
  webRequest: { onBeforeSendHeaders: evt(), onHeadersReceived: evt(), onBeforeRequest: evt() },
  contextMenus: { create: noop, onClicked: evt() }, windows: { onFocusChanged: evt(), create: async () => ({}) }, idle: { onStateChanged: evt() },
  scripting: { executeScript: async () => [] }, permissions: { contains: async () => true }, commands: { onCommand: evt() },
  bookmarks: { getTree: async () => [], onCreated: evt(), onRemoved: evt(), onChanged: evt(), onMoved: evt() } };
class Socket { constructor() { this.readyState = 0; } addEventListener() {} close() { this.readyState = 3; } send() {} }
if (scenario === 'user-pairing-wins') { store.cfg = { pubkey: Bk, key: Buffer.alloc(32, 9).toString('base64'), relay: 'wss://r', relays: [], mode: 'ro', source: 'user' }; managed = code(A, 1); }
if (scenario === 'paired-at-start' || scenario === 'signed-out' || scenario === 'account-switch' || scenario === 'unchanged') managed = code(A, 1);
const ctx = { browser: B, chrome: B, console, crypto: webcrypto, WebSocket: Socket, Date, TextEncoder, TextDecoder, URL, URLSearchParams,
  Uint8Array, Uint32Array, ArrayBuffer, DataView, Promise, Map, Set, WeakMap, WeakSet, Math, Number, Error, AbortController,
  setTimeout, clearTimeout, setInterval: noop, clearInterval: noop, queueMicrotask, performance,
  atob: s => Buffer.from(s, 'base64').toString('binary'), btoa: s => Buffer.from(s, 'binary').toString('base64'),
  navigator: { onLine: true, userAgent: 'Firefox' }, fetch: async () => ({ ok: false, json: async () => ({}) }) };
ctx.self = ctx; ctx.globalThis = ctx; vm.createContext(installStateGlobals(ctx) && ctx);
for (const file of ['vendor/nostr.bundle.js', 'vaultcore.js', 'background.js']) vm.runInContext(fs.readFileSync(path.join(dir, file), 'utf8'), ctx, { filename: file });
await vm.runInContext('ready', ctx);
const cfg = () => vm.runInContext('cfg', ctx);
const tick = async () => { alarm({ name: 'pcvault-sync' }); await new Promise(r => setTimeout(r, 50)); };
switch (scenario) {
  case 'paired-at-start':
    assert.equal(cfg() && cfg().pubkey, A, 'the browser was not paired from PosterChanOS at start');
    assert.equal(cfg().source, 'os'); assert.equal(cfg().mode, 'full'); assert.equal(store.cfg.pubkey, A);
    break;
  case 'later-sign-in':
    assert.equal(cfg(), null);
    managed = code(A, 1); await tick();
    assert.equal(cfg() && cfg().pubkey, A, 'a sign-in after the browser started was never picked up');
    break;
  case 'signed-out':
    managed = null; await tick();
    assert.equal(cfg(), null, 'signing out of PosterChanOS left this browser paired');
    break;
  case 'account-switch':
    managed = code(Bk, 2); await tick();
    assert.equal(cfg().pubkey, Bk, 'the browser kept the previous account after a switch');
    break;
  case 'unchanged': {
    const before = cfg(); await tick();
    assert.equal(cfg(), before, 're-paired from an identical code (it would drop the decrypted set every minute)');
    break; }
  case 'user-pairing-wins':
    assert.equal(cfg().pubkey, Bk); managed = null; await tick();
    assert.equal(cfg() && cfg().pubkey, Bk, 'a pairing the person made was replaced or removed');
    break;
  case 'no-managed-storage':
    assert.equal(cfg(), null); await tick(); assert.equal(cfg(), null);
    break;
  default: throw new Error('unknown scenario ' + scenario);
}
console.log('ok', scenario);
