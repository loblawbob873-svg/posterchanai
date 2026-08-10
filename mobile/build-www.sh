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
cp "$SRC"/static/css/client.css       www/static/css/
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
curl -fsSL https://poster.place/client -o www/index.html

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
  window.fetch = function(i, o){
    try {
      // Rewrite root-relative URLs to the server AND force credentials:'include' — these are cross-origin
      // (app origin https://localhost → poster.place), so without it the browser never sends the session
      // cookie nor stores Set-Cookie, and every authed call 401-loops (settings, etc.). Paired with the
      // server's SameSite=None cookie + CORS allow-credentials.
      if (typeof i === 'string' && i.charAt(0) === '/'){ i = B + i; o = _auth(Object.assign({}, o, {credentials:'include'})); }
      else if (i && i.url && i.url.charAt(0) === '/'){ o = _auth(Object.assign({}, o, {credentials:'include'})); i = new Request(B + i.url, i); }
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
print('www/index.html built (bundled mode injected)')
PY

echo "www assembled: $(find www -type f | wc -l) files"
