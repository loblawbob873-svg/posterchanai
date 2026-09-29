/* Run the SHIPPED static/js/client/streams.js phone-live overlay against a stub DOM and a fake
 * ScreenShare plugin, and check the two audio controls a phone screen share has:
 *
 *   "When I go live with my phone sharing screen, the 'mute' button mutes my mic AND the screen
 *    audio. I want to control it individually."
 *
 * The fake plugin models the SERVICE's two independent inputs (mic, screen) the way
 * ScreenShareService does after the fix, and every assertion reads the fake service's state — what
 * viewers would hear — not only the button text.
 *
 * The factory keeps its internals private, so this harness appends ONE line to the source before the
 * factory's `return {` that hands the internals to the test (the shipped file is not modified).
 * Prints one JSON line per check; the Python wrapper asserts on them.
 */
import fs from 'node:fs';
import path from 'node:path';

const SRC = path.resolve(process.argv[2]);
const results = [];
const check = (name, ok, detail) => results.push({ name, ok: !!ok, detail: detail === undefined ? '' : String(detail) });

// ------------------------------------------------------------------ tiny DOM
class ClassList {
  constructor(){ this.s = new Set(); }
  add(c){ this.s.add(c); } remove(c){ this.s.delete(c); } contains(c){ return this.s.has(c); }
  toggle(c, on){ const want = on === undefined ? !this.s.has(c) : !!on; if(want) this.s.add(c); else this.s.delete(c); return want; }
}
class El {
  constructor(tag){
    this.tagName = String(tag||'div').toUpperCase(); this.children = []; this.parentNode = null;
    this.classList = new ClassList(); this.id = ''; this.textContent = ''; this.title = '';
    this.hidden = false; this.onclick = null; this.style = {}; this.attrs = {}; this.srcObject = null;
  }
  set className(v){ String(v||'').split(/\s+/).filter(Boolean).forEach(c=>this.classList.add(c)); }
  setAttribute(k, v){ this.attrs[k] = String(v); }
  getAttribute(k){ return k in this.attrs ? this.attrs[k] : null; }
  play(){ return Promise.resolve(); }
  appendChild(c){ c.parentNode = this; this.children.push(c); return c; }
  remove(){ if(this.parentNode){ this.parentNode.children = this.parentNode.children.filter(x=>x!==this); this.parentNode = null; } }
  // Just enough of innerHTML: every element that carries an id becomes a (flat) child, with its
  // `hidden` attribute and its visible text (whatever follows an inline <svg>…</svg>).
  set innerHTML(html){
    this.children = [];
    const re = /<(\w+)([^>]*)>/g; let m;
    while((m = re.exec(html))){
      const idm = /\bid="([^"]+)"/.exec(m[2]); if(!idm) continue;
      const el = new El(m[1]); el.id = idm[1]; el.hidden = /\shidden(\s|$|>)/.test(m[2] + ' ');
      const cls = /\bclass="([^"]*)"/.exec(m[2]); if(cls) el.className = cls[1];
      const rest = html.slice(re.lastIndex);
      const close = rest.indexOf('</' + m[1] + '>');
      if(close >= 0) el.textContent = rest.slice(0, close).replace(/<svg[\s\S]*?<\/svg>/g, '').replace(/<[^>]+>/g, '');
      this.appendChild(el);
    }
  }
  _all(){ const out = []; for(const c of this.children){ out.push(c, ...c._all()); } return out; }
  querySelector(sel){ if(!sel.startsWith('#')) return null; return this._all().find(e=>e.id===sel.slice(1)) || null; }
}
const body = new El('body');
globalThis.document = {
  body, createElement: t => new El(t),
  getElementById: id => body._all().find(e=>e.id===id) || null,
  querySelector: s => body.querySelector(s),
};
globalThis.window = globalThis;
// the APK's WebView: no getDisplayMedia (node's own navigator is a getter-only global)
Object.defineProperty(globalThis, 'navigator', { value: { mediaDevices: {} }, configurable: true, writable: true });
globalThis.Store = { profile: () => ({}) };
globalThis.setInterval = () => 0;
globalThis.fetch = async () => { throw new Error('offline'); };

// ------------------------------------------------------------------ the fake capture service
const service = { running: false, mic: 'on', screen: 'on', screenAudio: true, startArgs: null };
let statusCb = null;
const ScreenShare = {
  async requestConsent(){ return { granted: true }; },
  addListener(ev, cb){ statusCb = cb; return Promise.resolve({ remove(){} }); },
  async start(opts){
    service.startArgs = opts; service.running = true;
    service.mic = opts.muted ? 'muted' : 'on';
    service.screen = opts.screenMuted ? 'muted' : 'on';
    setTimeout(()=>statusCb && statusCb({ event: 'connected' }), 0);
  },
  async stop(){ service.running = false; },
  async isStreaming(){ return { value: service.running }; },
  async setMuted({ muted }){ if(!service.running) throw new Error('not running'); service.mic = muted ? 'muted' : 'on'; return { muted }; },
  async setScreenMuted({ muted }){
    if(!service.running) throw new Error('not running');
    if(service.screenAudio) service.screen = muted ? 'muted' : 'on';
    return { muted, screenAudio: service.screenAudio };
  },
  async audioState(){ return { micMuted: service.mic === 'muted', screenAudio: service.screenAudio, screenMuted: service.screen === 'muted' }; },
};

// ------------------------------------------------------------------ load the SHIPPED factory
let src = fs.readFileSync(SRC, 'utf8');
const anchor = '  return {\n    _closeStreamChat';
if(!src.includes(anchor)){ console.log(JSON.stringify({ name: 'harness', ok: false, detail: 'factory return anchor moved' })); process.exit(0); }
src = src.replace(anchor, '  globalThis.__T={ get ps(){ return _phoneStream; }, set ps(v){ _phoneStream=v; }, set live(v){ _liveStream=v; }, _phoneLiveOverlay, _toggleMute,\n' +
  '    _toggleScreenAudio: typeof _toggleScreenAudio==="function" ? _toggleScreenAudio : null, _setMiniLive, _screenGoLive, _toggleScreen };\n' + anchor);
(0, eval)(src);

const toasts = [];
const $ = (sel, root) => (root || document).querySelector(sel);
const S = { ME: { pubkey: 'ab'.repeat(32) }, CFG: {} };
const known = {
  state: S, $, $$: () => [], _capPlugin: (name, method) => (name === 'ScreenShare' && (!method || ScreenShare[method])) ? ScreenShare : null,
  toast: m => toasts.push(m), isDesktop: () => false, switchView(){}, publish: async () => { throw new Error('no relay'); },
  enc: s => String(s), STREAM_RELAYS: [],
};
const dep = new Proxy(known, { get: (t, k) => (k in t ? t[k] : () => {}) });
window.PCStreamsFactory(dep);
const T = globalThis.__T;
const tick = () => new Promise(r => setTimeout(r, 5));
const btn = id => document.getElementById(id);
const lastToast = () => toasts[toasts.length - 1] || '';

// ================================================================== 1. native screen share
await T._screenGoLive({ token: 'tok', rtmp_native_url: 'rtmp://h/tok?key=k' }, 'my stream', false, '');
await tick();
check('native share is live', T.ps && T.ps.native && service.running, JSON.stringify(T.ps && { native: T.ps.native }));
// A missing button reads as hidden with no text, so every later check still RUNS (and fails) on old code.
const sa = () => btn('pl-screen-audio') || { hidden: true, textContent: '(no screen-audio button)' };
check('screen-audio button exists', !!btn('pl-screen-audio'));
check('screen-audio button shown for a native share with screen audio', sa() && !sa().hidden, sa() && sa().hidden);
check('mic button still says Mute', btn('pl-mute') && btn('pl-mute').textContent === '🎤 Mute', btn('pl-mute') && btn('pl-mute').textContent);

// mic toggle changes ONLY the mic
await T._toggleMute(); await tick();
check('mic toggle mutes the mic', service.mic === 'muted', service.mic);
check('mic toggle leaves the screen audio on air', service.screen === 'on', service.screen);
check('mic toast names the mic', /^mic muted/.test(lastToast()), lastToast());
check('mic button says Unmute', btn('pl-mute').textContent === '🔇 Unmute', btn('pl-mute').textContent);
check('screen button unchanged by the mic', sa().textContent === '🔊 Screen audio', sa().textContent);

// screen toggle changes ONLY the screen audio
if(T._toggleScreenAudio){ await T._toggleScreenAudio(); await tick(); }
check('screen toggle mutes the screen audio', service.screen === 'muted', service.screen);
check('screen toggle leaves the mic as it was', service.mic === 'muted', service.mic);
check('screen toast names the screen audio', /^screen audio muted/.test(lastToast()), lastToast());
check('screen button says muted', sa() && sa().textContent === '🔈 Screen muted', sa() && sa().textContent);

// unmute the mic: the screen stays muted
await T._toggleMute(); await tick();
check('unmuting the mic does not unmute the screen', service.mic === 'on' && service.screen === 'muted', service.mic + '/' + service.screen);

// both states survive minimize/restore and a full re-render of the overlay
T._setMiniLive(true); T._setMiniLive(false);
check('minimize keeps the buttons', btn('pl-mute').textContent === '🎤 Mute' && sa().textContent === '🔈 Screen muted',
      btn('pl-mute').textContent + ' | ' + sa().textContent);
await T._toggleMute(); await tick();     // mic muted again
T._phoneLiveOverlay();
check('re-render keeps the mic state', btn('pl-mute').textContent === '🔇 Unmute', btn('pl-mute').textContent);
check('re-render keeps the screen state', sa() && !sa().hidden && sa().textContent === '🔈 Screen muted', sa() && (sa().hidden + ' ' + sa().textContent));

// a device with no screen audio (Android 9 / playback capture refused): no screen button
// (_publishLive set _liveStream before its publish failed; clear both, as _endLive would.)
service.screenAudio = false; T.ps = null; T.live = null; service.running = false;
document.getElementById('phone-live') && document.getElementById('phone-live').remove();
await T._screenGoLive({ token: 'tok2', rtmp_native_url: 'rtmp://h/tok2?key=k' }, 't', false, '');
await tick();
check('no screen audio -> no screen button', T.ps && sa() && sa().hidden, sa() && sa().hidden);

// ================================================================== 2. camera stream
T.ps = null; T.live = null; document.getElementById('phone-live') && document.getElementById('phone-live').remove();
service.screenAudio = true; service.running = false;
const micTrack = { kind: 'audio', enabled: true, stop(){} };
const vidTrack = { kind: 'video', enabled: true, stop(){}, getSettings: () => ({}) };
const local = { getAudioTracks: () => [micTrack], getVideoTracks: () => [vidTrack], getTracks: () => [micTrack, vidTrack] };
const pc = { close(){}, getSenders: () => [], onconnectionstatechange: null };
T.ps = { pc, local, token: 'cam', facing: 'user', source: 'camera', info: { rtmp_native_url: 'rtmp://h/cam?key=k' } };
T._phoneLiveOverlay();
check('camera stream never shows the screen button', sa() && sa().hidden, sa() && sa().hidden);
await T._toggleMute();
check('camera mute disables the mic track', micTrack.enabled === false, micTrack.enabled);

// camera (muted) -> native screen share: both mutes ride the start call (pre-air)
await T._toggleScreen(); await tick();
check('switch to screen carries the mic mute', service.startArgs && service.startArgs.muted === true, JSON.stringify(service.startArgs));
check('switch to screen carries a screen mute field', service.startArgs && 'screenMuted' in service.startArgs, JSON.stringify(service.startArgs));
check('after the switch the screen button appears', sa() && !sa().hidden, sa() && sa().hidden);
check('after the switch the mic is still muted on air', service.mic === 'muted', service.mic);

for(const r of results) console.log(JSON.stringify(r));
process.exit(0);
