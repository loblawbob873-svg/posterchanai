'use strict';
/* THE TRAY AGAINST THE BRIDGES THE DESKTOP REALLY EXPOSES.
 *
 * Every tray test built its own `pcAudio`/`pcNet`/… as ordinary objects. The desktop never hands the
 * page one of those: desktop/preload.js goes through Electron's contextBridge, which gives the page
 * a DEEP-FROZEN copy (read-only, non-configurable). A Proxy over pcAudio passed every test and threw
 * on every read in the app — the tray lost its Quick Settings button (sound, Wi-Fi, power) on both
 * monitors, with nothing logged.
 *
 * So this runs the SHIPPED preload.js against a fake `electron` whose contextBridge does what the
 * real one does (freeze, define non-writable on window), with only ipcRenderer.invoke answered by a
 * fake machine. Every bridge, every method name and every wrapper in preload is the real one. Then
 * it loads the SHIPPED osshell.js on that window and asks the tray for what a person sees.
 */
const fs = require('fs'), path = require('path'), vm = require('vm');
const ROOT = path.join(__dirname, '..', '..');
const PRELOAD = path.join(ROOT, 'desktop', 'preload.js');
const OSSHELL = path.join(ROOT, 'static', 'js', 'client', 'osshell.js');

const machine = { percent: 40, muted: false, invoked: [] };
function answer(channel){
  machine.invoked.push(channel);
  if(/^pc:audio:status$/.test(channel)) return { output: { percent: machine.percent, muted: machine.muted }, input: { percent: 80, muted: false }, sinks: [], sources: [] };
  if(/^pc:audio:mute$/.test(channel)){ machine.muted = !machine.muted; return { ok: true }; }
  if(/^pc:net:status$/.test(channel)) return { online: true, kind: 'wired', name: 'Wired' };
  if(/^pc:power:status$/.test(channel)) return { battery: { present: false } };
  if(/^pc:wm:windows$/.test(channel)) return [];
  if(/status$/.test(channel)) return {};
  return null;
}
function deepFreeze(o){
  if(o && (typeof o === 'object' || typeof o === 'function') && !Object.isFrozen(o)){
    Object.freeze(o);
    for(const k of Object.getOwnPropertyNames(o)){ try{ deepFreeze(o[k]); }catch(_){ } }
  }
  return o;
}

const store = new Map();
const win = {
  localStorage: { getItem: k => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) },
  addEventListener(){}, removeEventListener(){}, setInterval: () => 0, clearInterval(){}, setTimeout, clearTimeout,
  innerWidth: 1920, innerHeight: 1080, location: { href: 'app://posterchan/index.html', search: '' },
  document: { getElementById: () => null, createElement: () => ({ style: {}, classList: { add(){}, remove(){} }, appendChild(){}, setAttribute(){} }),
              addEventListener(){}, body: { classList: { add(){}, remove(){}, contains: () => false }, appendChild(){} },
              documentElement: { appendChild(){}, classList: { add(){}, remove(){} } }, getElementsByTagName: () => [] },
  navigator: { userAgent: 'PosterChanOS' }, opener: null,
};
const exposed = [];
const electron = {
  contextBridge: { exposeInMainWorld(name, api){
    exposed.push(name);
    Object.defineProperty(win, name, { value: deepFreeze(api), writable: false, configurable: false, enumerable: true });
  } },
  ipcRenderer: { invoke: async (ch) => answer(ch), send(){}, sendSync: () => null, on(){}, once(){}, removeListener(){}, removeAllListeners(){} },
  webFrame: { setZoomFactor(){}, getZoomFactor: () => 1, setVisualZoomLevelLimits(){} },
};
const preloadCtx = vm.createContext(Object.assign(Object.create(null), {
  require: (m) => m === 'electron' ? electron : require(m),
  process: { argv: ['electron', '--shell'], platform: 'linux', env: {}, versions: process.versions, arch: process.arch },
  window: win, document: win.document, console, setTimeout, clearTimeout, setInterval: () => 0, clearInterval(){},
  Buffer, URL, TextEncoder, TextDecoder, Promise, JSON, Math, Date, Object, Array, String, Number, Error, Map, Set,
  navigator: win.navigator, location: win.location, localStorage: win.localStorage, globalThis: win,
}));
try{ vm.runInContext(fs.readFileSync(PRELOAD, 'utf8'), preloadCtx, { filename: 'preload.js' }); }
catch(e){ console.error('FAIL preload.js did not run under the fake electron: ' + (e && e.stack || e)); process.exit(1); }

function ok(name, v, detail){ if(!v){ console.error('FAIL ' + name + (detail ? ' :: ' + detail : '')); process.exit(1); } console.log('  ok   ' + name); }
ok('preload exposes the tray bridges', ['pcAudio', 'pcNet', 'pcPower', 'pcWM', 'pcPopup'].every(n => exposed.includes(n)), exposed.join(','));
ok('and they are frozen like contextBridge makes them', Object.isFrozen(win.pcAudio) && !Object.getOwnPropertyDescriptor(win, 'pcAudio').writable);

const shellCtx = vm.createContext(Object.assign(win, { Promise, JSON, Math, Date, String, Number, Array, Object, Map, Set, Proxy, Error, console, window: win }));
vm.runInContext(fs.readFileSync(OSSHELL, 'utf8'), shellCtx, { filename: 'osshell.js' });
const S = win.PCOSShell;

(async () => {
  ok('a compositor answers', await S.detect());
  let state = null, err = '';
  try{ state = await S.panelState(); }catch(e){ err = String(e && e.message || e); }
  ok('the tray can read the machine through the real bridges', !!state && !err, err);
  const sum = S.panelSummary(state);
  ok('the sound level is known', sum.volume && sum.volume.known !== false && sum.volume.percent === 40, JSON.stringify(sum.volume));
  const html = S.panelHTML(sum);
  ok('Quick Settings draws its sound control', /data-os="quick"/.test(html) && /vol|sound|speaker/i.test(html), html.slice(0, 300));
  await S.refresh();
  const quick = S.quickHTML(sum, { shot: { ok: false, region: false } });
  ok('the flyout offers the volume slider', /type="range"|data-os="vol|volume/i.test(quick));
  ok('and the power tile', /data-os="power"/.test(quick));
  let muteErr = '';
  try{ await S.audio().setMuted(true, 'sink'); }catch(e){ muteErr = String(e && e.message || e); }
  ok('a mute goes through the frozen bridge', !muteErr && machine.invoked.includes('pc:audio:mute'), muteErr);
  console.log('OK tray with the real bridges');
})().catch(e => { console.error('FAIL ' + (e && e.stack || e)); process.exit(1); });
