#!/bin/bash
# Assemble the Capacitor web bundle (www/) from the SAME web-client files the site serves, then inject
# "bundled mode": every root-relative fetch/WebSocket is rewritten to https://poster.place, so the app
# runs its UI locally (a real app, offline shell) while data (relay, API, Blossom) comes from the server.
# The web UI and the app therefore stay identical — this runs on every deploy (sync.sh / CI).
set -e
cd "$(dirname "$0")"
SRC="$(cd .. && pwd)"

rm -rf www
mkdir -p www/static/js/client www/static/css www/static/fonts

cp "$SRC"/static/js/client/*.js       www/static/js/client/
# EVERY STYLESHEET THE SHELL REFERENCES, READ FROM THE TEMPLATE — never a list maintained here.
#
# This was three hardcoded names, and `templates/client.html` grew a fourth. `monero-wallet.css`
# was therefore present on the web (the server serves /static) and ABSENT from the bundle, where
# the shim treats /static/ as bundle-local — so the request 404'd inside the app and the whole
# Monero wallet rendered unstyled. On the web it was perfect. Reported exactly that way: "monero
# works fine on web, android is broken".
#
# Same shape as the fonts and the i18n catalogues below, which is three times now, so the list is
# derived instead of restated. A stylesheet added to the shell is copied without anyone remembering.
for _css in $(grep -o 'href="/static/css/[^"?]*' "$SRC/templates/client.html" | sed 's|.*/||' | sort -u); do
  cp "$SRC/static/css/$_css" www/static/css/ || { echo "build-www: missing static/css/$_css" >&2; exit 1; }
done
# Loaded at RUNTIME by i18n.js for right-to-left languages, so it is in no template and the loop
# above cannot see it.
cp "$SRC"/static/css/rtl.css          www/static/css/
# The translation catalogues. i18n.js fetches /static/i18n/<lang>.json at runtime, and in a bundle
# that request is served by the bundle — so without this the language picker offers Arabic and
# Japanese, the fetch 404s, and the client falls back to English with nothing on screen to say why.
# Same shape as the fonts above: present on the web because the server serves them, missing in the
# app because nobody copied them.
mkdir -p www/static/i18n
cp "$SRC"/static/i18n/*.json          www/static/i18n/ 2>/dev/null || true
# client.css @font-face's Inter and Orbitron from /static/fonts. Those root-relative urls live INSIDE a
# stylesheet, so the fetch shim never sees them — the WebView resolves them against the page origin and
# they 404 against the bundle, silently dropping the whole app to a system font. Present on the web (the
# server serves them) and missing in the app, which is why it went unnoticed.
cp "$SRC"/static/fonts/*.woff2        www/static/fonts/ 2>/dev/null || true
# The service worker must sit at the bundle ROOT so it can register with root scope (the app loads the
# client at / — not /client like the web PWA). Without this the SW never registered in the app and the
# media cache never ran (media re-downloaded every view). app.js registers /sw.js when in-app.
cp "$SRC"/static/js/client/sw.js      www/sw.js
# The WHOLE vendor tree, not a hand-picked list. Each of these is pulled in by a root-relative
# <script src>/<link href>, which the bundled-mode fetch shim never sees — the WebView resolves them
# against the page, so anything not copied 404s and the feature dies with nothing useful in the console.
# The enumerated list is how jsqr (QR SCANNING — the "scan a signer QR" login) and katex (the maths in
# Flashcards) were silently missing from the APK for as long as it has existed: both load ON DEMAND, so
# nothing breaks until someone opens that one screen. Copying the directory can't forget the next one.
cp -r "$SRC"/static/vendor            www/static/
# Every image the client can reference by URL, not just PNGs. `client.css` asks for
# /static/os-wallpaper.webp, which no glob here matched, so the bundled apps lost the desktop-mode
# wallpaper while the website kept it — the same shape as the missing fonts above, and invisible for
# the same reason: a 404 inside a stylesheet says nothing on screen except that something is gone.
# tests/test_bundle_assets.py fails if the client starts asking for a type this does not carry.
cp "$SRC"/static/*.png "$SRC"/static/*.webp "$SRC"/static/*.svg \
   "$SRC"/static/*.jpg "$SRC"/static/*.ico  www/static/ 2>/dev/null || true

# The rendered shell (auth gate + app scaffold) — take the LIVE one so the app matches the site exactly.
curl --fail --silent --show-error --location \
  --retry 5 --retry-all-errors --retry-delay 2 \
  https://poster.place/client -o www/index.html
test -s www/index.html

# Inject bundled-mode (API base + fetch/WS shim) right before </head>, ahead of every app script. Also
# strip cache-busting ?v= from local asset URLs (served from the bundle, not the network) and drop the
# manifest link (native app, not a PWA install).
python3 - <<'PY'
import re, os
p = 'www/index.html'
html = open(p, encoding='utf-8').read()
# Bake THIS build's number (GitHub run_number == versionCode, passed as PC_APP_BUILD) into the bundle so the
# in-app updater can compare it against /apk/version and offer a download when the server has a newer APK.
_b = (os.environ.get('PC_APP_BUILD', '0') or '0').strip()
build = _b if _b.isdigit() else '0'
shim = '''<script>
// The instance this app talks to. Defaults to poster.place, but a user can point a fresh install at ANY
// self-hosted PosterChan instance (Settings / the login screen's instance field) — stored in localStorage
// and read here before any request, so the whole app (API, relay, blossom) targets their chosen domain.
window.__PC_API_BASE__ = (function(){ try{ var s=localStorage.getItem('pc_instance'); if(s) return String(s).replace(/\\/+$/,''); }catch(e){} return 'https://poster.place'; })();
window.__PC_SET_INSTANCE__ = function(u){ try{ localStorage.setItem('pc_instance', u); }catch(e){} try{ location.reload(); }catch(e){} };
// Bearer token for the instance, set by app.js (_setAiToken) once nostr-login returns one, and read
// per-request by _auth below so a token acquired mid-session applies immediately. Cookies are not
// usable against a .onion instance — it is plain http, and SameSite=None demands Secure, which the
// WebView refuses over a non-HTTPS connection — so the header is the only auth that works there.
// In-memory only: a persisted token would survive a switch of identity or instance.
window.__PC_TOKEN__ = '';
window.__PC_APP_BUILD__ = __BUILD__;
(function(){
  // An APK update can start while the previous APK's service worker still controls this first page.
  // Listen before any app modules load; once the newly bundled worker claims the page, reload exactly
  // once onto its fresh shell. This preserves IndexedDB/localStorage/media while preventing a new APK
  // from running old JS and CSS until the user manually force-closes it.
  if (!('serviceWorker' in navigator) || !navigator.serviceWorker.controller) return;
  var changed = false;
  navigator.serviceWorker.addEventListener('controllerchange', function(){
    if (changed) return; changed = true;
    try { location.reload(); } catch(e) {}
  });
})();
(function(){
  var B = window.__PC_API_BASE__, W = B.replace(/^http/, 'ws');
  var _f = window.fetch.bind(window);
  // Attach the instance bearer token, but NEVER over an Authorization the caller already set — Blossom
  // and NIP-98 uploads carry their own `Nostr <base64>` header, and clobbering it would break uploads.
  function _auth(o){
    try{
      var t = window.__PC_TOKEN__; if(!t) return o;
      var h = o.headers;
      if (h && typeof h.get === 'function'){ if(!h.get('Authorization')) h.set('Authorization', 'Bearer '+t); return o; }
      h = Object.assign({}, h || {});
      var has = false; for (var k in h){ if (String(k).toLowerCase() === 'authorization') has = true; }
      if (!has) h['Authorization'] = 'Bearer '+t;
      o.headers = h; return o;
    }catch(e){ return o; }
  }
  // NOT EVERY ROOT-RELATIVE URL IS A SERVER CALL: the bundle serves its OWN assets at exactly those
  // paths, and they must be read from the bundle, not from the instance. Almost everything under
  // /static/ is pulled in by a <script>/<link>/@font-face, which the WebView resolves against the page
  // and this shim never sees — so the omission stayed invisible until something fetch()ed one. The
  // translation catalogues are that something: `fetch('/static/i18n/ar.json')` was rewritten to
  // https://poster.place/static/i18n/ar.json — cross-origin from https://localhost, with no
  // Access-Control-Allow-Origin on static files (and credentials:'include' forced below, which even a
  // wildcard could not satisfy). It failed as a TypeError, i18n.js caught it, and the APK answered
  // "could not load that language — staying in English" while the file sat in the bundle the whole
  // time. The desktop shim has always had this guard; the APK's never did.
  function isLocal(p){ return p.indexOf('/static/') === 0 || p === '/sw.js' || p === '/index.html'; }
  window.fetch = function(i, o){
    try {
      // Rewrite root-relative URLs to the server AND force credentials:'include' — these are cross-origin
      // (app origin https://localhost → poster.place), so without it the browser never sends the session
      // cookie nor stores Set-Cookie, and every authed call 401-loops (settings, etc.). Paired with the
      // server's SameSite=None cookie + CORS allow-credentials.
      var _p = (typeof i === 'string') ? i : (i && i.url) || '';
      if (_p.charAt(0) === '/' && !isLocal(_p)){
        if (typeof i === 'string'){ i = B + i; o = _auth(Object.assign({}, o, {credentials:'include'})); }
        else { o = _auth(Object.assign({}, o, {credentials:'include'})); i = new Request(B + i.url, i); }
      }
    } catch(e){}
    return _f(i, o);
  };
  var _WS = window.WebSocket;
  window.WebSocket = function(u, p){ if (typeof u === 'string' && u.charAt(0) === '/') u = W + u; return new _WS(u, p); };
  window.WebSocket.prototype = _WS.prototype;
  // Mark the native app so CSS can reserve the system status-bar height at the top — the WebView is
  // edge-to-edge and tablets report env(safe-area-inset-top)=0, so the login logo / topbar otherwise sit
  // under the status-bar clock/date (the 'date over logo' bug).
  document.addEventListener('DOMContentLoaded', function(){ try{ document.body.classList.add('native'); }catch(e){} });
})();
</script>
'''
shim = shim.replace('__BUILD__', build)
html = html.replace('</head>', shim + '</head>', 1)
# local assets are bundled at their /static/... paths — drop the ?v=NNN query so the local server serves them
html = re.sub(r'(/static/[^"\'?\s]+)\?v=[0-9]+', r'\1', html)
# manifest is a PWA concept; harmless but 404s locally — remove it
html = re.sub(r'<link[^>]+rel=["\']manifest["\'][^>]*>', '', html)
# STRIP upgrade-insecure-requests. The shell is fetched over HTTPS, so the server emits that CSP (it's
# right for the web PWA: the page is https and mixed content would be blocked). Baked into the APK it is
# actively wrong — the bundle's page is https://localhost but the INSTANCE may legitimately be cleartext
# (an .onion, which is plain HTTP by design, or a LAN box), and the CSP silently rewrites every fetch /
# WebSocket / <img> to https://<that host>, which does not exist. The app is allowed to speak cleartext
# on purpose (usesCleartextTraffic + allowMixedContent); this meta would override that for no benefit.
html = re.sub(r'<meta[^>]+upgrade-insecure-requests[^>]*>', '', html, flags=re.I)
open(p, 'w', encoding='utf-8').write(html)
# Give every APK build its own SHELL cache. The source cache number still controls web-PWA releases;
# the build suffix prevents two consecutive APKs from sharing a cache merely because sw.js itself did
# not otherwise change. Media/drive caches retain their separate stable names and are not discarded.
sw = 'www/sw.js'
sw_text = open(sw, encoding='utf-8').read()
sw_text, changed = re.subn(r"const CACHE = '([^']+?)(?:-apk\d+)?';",
                           lambda m: "const CACHE = '" + m.group(1) + "-apk" + build + "';",
                           sw_text, count=1)
if changed != 1:
    raise SystemExit('could not stamp APK service-worker cache')
open(sw, 'w', encoding='utf-8').write(sw_text)
# THIS BUNDLE'S OWN COMMIT, overwriting whatever the server had when index.html was fetched.
# A bundle is built from a checkout and then installed by hand, so "which build is this device on"
# has to be answerable from the device — it is the question that made every folder-sync report
# ambiguous until now.
import subprocess as _sp
try:
    _sha = _sp.run(['git','rev-parse','--short','HEAD'], capture_output=True, text=True,
                   timeout=5).stdout.strip() or 'unknown'
except Exception:
    _sha = 'unknown'
_h = open('www/index.html', encoding='utf-8').read()
_h2 = re.sub(r'window\.__PC_BUILD="[^"]*"', 'window.__PC_BUILD="' + _sha + '"', _h, count=1)
if _h2 == _h:
    _h2 = _h.replace('<head>', '<head><script>window.__PC_BUILD="' + _sha + '";</script>', 1)
open('www/index.html','w',encoding='utf-8').write(_h2)
print('build stamp: ' + _sha)
print('www/index.html built (bundled mode injected)')
PY

echo "www assembled: $(find www -type f | wc -l) files"
