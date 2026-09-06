/* Load the SHIPPED background scripts with the browser refusing blocking webRequest -- what Firefox
   MV3 does to an ordinary add-on -- and report whether runtime.onMessage still gets a listener. */
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';

const dir = path.resolve(process.argv[2] || 'extension') + '/';
const manifest = JSON.parse(fs.readFileSync(dir + 'manifest.json', 'utf8'));
const refuse = process.argv[3] === 'refuse';

let listeners = 0, warned = [];
const noop = () => {};
const evt = () => ({ addListener: noop, removeListener: noop, hasListener: () => false });
const B = {
  runtime: {
    onMessage: { addListener: () => { listeners++; }, removeListener: noop },
    onInstalled: evt(), onStartup: evt(), onConnect: evt(), onSuspend: evt(),
    getURL: (p) => 'moz-extension://abc/' + (p || ''), id: 'passwords@poster.place',
    getManifest: () => manifest, getPlatformInfo: async () => ({ os: 'linux' }),
    lastError: null, sendMessage: async () => ({}),
  },
  storage: { local: { get: async () => ({}), set: async () => {}, remove: async () => {} },
             session: { get: async () => ({}), set: async () => {} }, onChanged: evt() },
  alarms: { create: noop, clear: noop, onAlarm: evt(), get: async () => null },
  tabs: { query: async () => [], sendMessage: async () => ({}), onUpdated: evt(), onRemoved: evt(),
          create: async () => ({}), onActivated: evt() },
  action: { setBadgeText: noop, setBadgeBackgroundColor: noop, setTitle: noop, onClicked: evt() },
  webRequest: { onBeforeSendHeaders: { addListener: () => {
                  if (refuse) throw new Error("Using the 'blocking' extraInfoSpec requires the "
                                              + "'webRequestBlocking' permission"); } },
                onHeadersReceived: evt(), onBeforeRequest: evt() },
  bookmarks: { onCreated: evt(), onRemoved: evt(), onChanged: evt(), onMoved: evt(),
               getTree: async () => [], search: async () => [] },
  scripting: { executeScript: async () => [] },
  contextMenus: { create: noop, onClicked: evt(), removeAll: noop },
  windows: { onFocusChanged: evt() }, idle: { onStateChanged: evt() },
  permissions: { contains: async () => true }, commands: { onCommand: evt() },
};
// Timers are stubbed so a reconnect loop cannot keep this process alive.
const ctx = {
  browser: B, chrome: B,
  console: { log: noop, warn: (...a) => warned.push(String(a[0])), error: noop, debug: noop, info: noop },
  WebSocket: class { constructor(){ this.readyState = 0; } send(){} close(){} addEventListener(){} },
  crypto: globalThis.crypto, TextEncoder, TextDecoder, URL, URLSearchParams,
  setTimeout: () => 0, clearTimeout: noop, setInterval: () => 0, clearInterval: noop,
  queueMicrotask, fetch: async () => ({ ok: false, json: async () => ({}) }),
  atob: (s) => Buffer.from(s, 'base64').toString('binary'),
  btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
  performance, Date, Math, JSON, Promise, Map, Set, WeakMap, WeakSet, Array, Object, String, Number,
  Boolean, Error, TypeError, RangeError, Symbol, Uint8Array, Uint32Array, ArrayBuffer, DataView,
  isNaN, parseInt, parseFloat, encodeURIComponent, decodeURIComponent, structuredClone,
  location: { href: 'moz-extension://abc/bg.html', origin: 'moz-extension://abc' },
  navigator: { userAgent: 'Mozilla/5.0 Firefox/128.0', onLine: true },
  document: { addEventListener: noop, createElement: () => ({ style: {}, setAttribute: noop }),
              documentElement: {}, body: { appendChild: noop } },
};
ctx.self = ctx; ctx.window = ctx; ctx.globalThis = ctx;
vm.createContext(ctx);

const loaded = [];
let failure = null;
for (const s of manifest.background.scripts) {
  try { vm.runInContext(fs.readFileSync(dir + s, 'utf8'), ctx, { filename: s, timeout: 20000 });
        loaded.push(s); }
  catch (e) { failure = { script: s, message: (e && e.message) || String(e) }; break; }
}
console.log(JSON.stringify({ refused: refuse, loaded, failure, messageListeners: listeners,
                             warned: warned.slice(0, 3) }));
