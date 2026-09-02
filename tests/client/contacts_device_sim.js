/* Open ⋯ → Addressbooks from the SHIPPED contacts.js, under node, against a HALF-ARRIVED Capacitor.
 *
 * The bug this exists for: on the packaged APK the phone-book row rendered as an EMPTY STRING —
 * neither the switch nor the sentence written to explain its absence — because the only gate on
 * showing anything was `Capacitor.getPlatform()`, i.e. the very piece that can be missing. A bridge
 * that never arrived then looked exactly like Chrome, and a detection bug looked exactly like a build
 * that predates the feature. No grep can catch that: it is what several objects being absent
 * TOGETHER does to one function, so the only honest test is to run the real one in each of those
 * worlds.
 *
 * The plugin lookup under test is the SHIPPED `_capPlugin` (and its raw-channel fallback), lifted out
 * of static/js/client/app.js by source — app.js is one enormous IIFE and cannot be required, but a
 * hand-copied lookup would be testing this file instead of the app.
 *
 * Usage:  node contacts_device_sim.js '<json options>'   → prints JSON on stdout.
 *   env: full | plugin-map-only | raw-bridge | raw-bridge-no-plugin | no-capacitor |
 *        no-plugin | web-on-android | browser
 */
const fs = require('fs');
const path = require('path');
const ROOT = path.resolve(__dirname, '..', '..');

const opt = JSON.parse(process.argv[2] || '{}');
const ENV = opt.env || 'full';

/* ---- the shipped plugin lookup ---------------------------------------------------------------- */
const APPJS = fs.readFileSync(path.join(ROOT, 'static', 'js', 'client', 'app.js'), 'utf8');

function grab(header){
  const i = APPJS.indexOf(header);
  if(i < 0) throw new Error('app.js no longer contains: ' + header);
  let depth = 0;
  for(let k = APPJS.indexOf('{', i); k < APPJS.length; k++){
    if(APPJS[k] === '{') depth++;
    else if(APPJS[k] === '}'){ depth--; if(!depth) return APPJS.slice(i, k + 1); }
  }
  throw new Error('unbalanced braces after: ' + header);
}
const STATE = (APPJS.match(/^\s*let _rawSeq = .*$/m) || [''])[0];
const SRC = STATE + '\n' + grab('function _rawNative(){') + '\n' + grab('function _capPlugin(name, method){');
// eslint-disable-next-line no-eval
const _capPlugin = eval('(function(){' + SRC + '\nreturn _capPlugin;})()');

/* ---- the phone ------------------------------------------------------------------------------- */
const STATUS = { granted:false, account:false, owner:'', count:0 };
const nativeCalls = [];

function pluginMap(){                       // what Capacitor Android injects for a Java-only plugin
  const call = (m) => (opts) => { nativeCalls.push([m, opts || {}]); return Promise.resolve(m === 'status' ? STATUS : {}); };
  return { ContactSync: { status: call('status'), begin: call('begin'), enable: call('enable'),
                          disable: call('disable'), pull: call('pull'), taken: call('taken'),
                          put: call('put'), commit: call('commit') } };
}

// The WebView's own message channel — a Java object, present before any script runs.
function androidBridge(hasPlugin){
  const ab = {
    onmessage: null,
    postMessage(s){
      const m = JSON.parse(s);
      nativeCalls.push([m.methodName, m.options || {}]);
      setTimeout(() => {
        if(!ab.onmessage) return;
        const ok = hasPlugin !== false;
        ab.onmessage({ data: JSON.stringify({ callbackId:m.callbackId, pluginId:m.pluginId,
                                              methodName:m.methodName, success: ok,
                                              data: (ok && m.methodName === 'status') ? STATUS : undefined,
                                              error: ok ? undefined : { message:'Plugin not implemented' } }) });
      }, 1);
    },
  };
  return ab;
}

/* ---- the worlds ------------------------------------------------------------------------------ */
global.window = global;
global.document = { createElement(){ return { innerHTML:'', firstElementChild:null }; } };
global.requestAnimationFrame = (fn) => setTimeout(fn, 0);
const UA_ANDROID = 'Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 Chrome/120 Mobile Safari/537.36 wv';
const UA_DESKTOP = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36';
// defineProperty, not assignment: node ≥21 ships its own read-only `navigator`, so `global.navigator =`
// is silently ignored and every UA-dependent branch here would be tested against "Node.js/xx".
const setUA = (ua) => Object.defineProperty(global, 'navigator', { value:{ userAgent:ua }, configurable:true, writable:true });
setUA(UA_ANDROID);

if(ENV === 'full'){
  // Everything arrived: bridge JS + the injected plugin map.
  global.Capacitor = { getPlatform: () => 'android', nativePromise: () => Promise.reject(new Error('unused')),
                       Plugins: pluginMap() };
  global.androidBridge = androidBridge();
  global.__PC_APP_BUILD__ = 986;
}else if(ENV === 'plugin-map-only'){
  // globalJS + plugin JS ran, native-bridge.js did not: no getPlatform, no nativePromise.
  global.Capacitor = { DEBUG:false, Plugins: pluginMap() };
  global.androidBridge = androidBridge();
  global.__PC_APP_BUILD__ = 986;
}else if(ENV === 'raw-bridge'){
  // NOTHING of Capacitor's JS reached the page. Only Java's own channel is there.
  global.androidBridge = androidBridge();
  global.__PC_APP_BUILD__ = 986;
}else if(ENV === 'raw-bridge-no-plugin'){
  // Java's channel is up, the plugin is not registered on the other side of it, and none of
  // Capacitor's JS reached the page. Nothing can make the switch work here — the panel's whole job
  // is to SAY so, and this is the world in which it used to say nothing.
  global.androidBridge = androidBridge(false);
  global.__PC_APP_BUILD__ = 986;
}else if(ENV === 'no-capacitor'){
  // The bundle, on an Android phone, with no native anything reachable at all.
  global.__PC_APP_BUILD__ = 986;
  global.__PC_API_BASE__ = 'https://poster.place';
}else if(ENV === 'no-plugin'){
  // A real, working bridge — for an APK built before this feature existed.
  global.Capacitor = { getPlatform: () => 'android', Plugins: { App:{}, Camera:{} },
                       nativePromise: () => Promise.reject(new Error('no such plugin')) };
  global.androidBridge = androidBridge();
  global.__PC_APP_BUILD__ = 900;
}else if(ENV === 'web-on-android'){
  // The web PWA in Chrome on a phone: an Android UA, but no bundle and no bridge. CardDAV IS the
  // answer here, so the row must stay away.
  setUA(UA_ANDROID);
}else{                                       // 'browser'
  setUA(UA_DESKTOP);
}

/* ---- the client stub ------------------------------------------------------------------------- */
require(path.join(ROOT, 'static', 'js', 'client', 'vcard.js'));
global.ClientSettings = { _v:{}, get(k, d){ return (k in this._v) ? this._v[k] : d; }, set(k, v){ this._v[k] = v; } };

let modalHtml = '';
const enc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
// A selector answers from the html the modal was actually given, so a handler is only wired to
// something that exists — and #ctb-phonebook being absent stays absent, which is the whole question.
const $ = (sel) => (typeof sel === 'string' && sel[0] === '#' && modalHtml.includes('id="' + sel.slice(1) + '"'))
  ? { onclick:null, onchange:null, checked:false, value:'', click(){}, textContent:'' } : null;

global.__PC = {
  VIEW: opt.action === 'add-phone' ? 'contacts' : 'timeline',
  $, $$: () => [],
  enc,
  toast(){}, closeModal(){},
  modal(html, onMount){ modalHtml = String(html || ''); if(onMount) onMount({}); },
  uiConfirm: async () => true,
  ensureAiSession: async () => {},
  authFetch: async (url) => ({ ok:true, status:200, json: async () => (/\/books/.test(url)
    ? { books: opt.action === 'add-phone' ? [{id:'personal', name:'Personal'}] : [] }
    : /\/cards/.test(url) ? { cards:[] } : {}) }),
  me: () => ({ pubkey:'me' }),
  capPlugin: _capPlugin,
};

require(path.join(ROOT, 'static', 'js', 'client', 'contacts.js'));

(async () => {
  if(opt.action === 'add-phone'){
    await global.PCContacts.reload();
    await global.PCContacts.addPhone(opt.phone || '');
  }else{
    await global.PCContacts.openMenu();
  }
  await new Promise(r => setTimeout(r, 30));
  const row = /class="[^"]*ct-phonebook/.test(modalHtml);
  console.log(JSON.stringify({
    env: ENV,
    row,                                                   // is there a phone-book row at all
    hasSwitch: modalHtml.includes('id="ctb-phonebook"'),   // …the working switch
    hasRetry: modalHtml.includes('id="ctb-phoneretry"'),   // …or a reason plus a way to re-ask
    saysUpdate: /Update the app/i.test(modalHtml),
    saysBridge: /native bridge/i.test(modalHtml),
    why: (modalHtml.match(/id="ctb-phonewhy">([^<]*)</) || [, ''])[1],
    nativeCalls,
    prefilledPhone: (modalHtml.match(/class="input ct-mv" value="([^"]*)"/) || [, ''])[1],
    html: opt.html ? modalHtml : undefined,
  }));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
