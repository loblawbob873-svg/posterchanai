#!/bin/bash
# Assemble the desktop web bundle (desktop/www/) from the SAME web-client files the site serves, then
# inject "bundled mode": every root-relative fetch/WebSocket is rewritten to whichever instance the user
# chose, and when they chose none, it fails fast instead. The app therefore runs its UI from disk and
# needs no server to start — a relay and a key are enough.
#
# WHY THE SHELL IS RENDERED HERE and not curl'd from poster.place the way mobile/build-www.sh does it:
# the whole point of this build is an app that does not depend on a PosterChan instance, and a build step
# that dies when production is down (or bakes production's settings into every installer) contradicts
# that on the first line. templates/client.html uses four Jinja values and no logic beyond `if
# nostr_only`, so rendering it here is a substitution, not a template engine — and `nostr_only` must NOT
# be baked in at all now: one bundle serves every instance, so the client decides it at RUNTIME from
# /client/config (or unconditionally when there is no instance).
set -e
cd "$(dirname "$0")"
SRC="$(cd .. && pwd)"

rm -rf www
mkdir -p www/static/js/client www/static/css www/static/fonts

cp "$SRC"/static/js/client/*.js       www/static/js/client/
cp "$SRC"/static/css/client.css       www/static/css/
# client.css @font-face's Inter and Orbitron from /static/fonts. Those are ROOT-RELATIVE urls inside a
# stylesheet, which the fetch shim does not touch (the browser resolves them against the page), so
# without the files here they 404 against the bundle and the whole app silently falls back to a system
# font — every heading, badge and button in the wrong face. Caught by check_desktop_standalone.py.
cp "$SRC"/static/fonts/*.woff2        www/static/fonts/ 2>/dev/null || true
# The service worker sits at the bundle ROOT: the app loads the client at / (not /client like the web
# PWA), so root scope is the only scope that covers it. app.js registers /sw.js when bundled.
cp "$SRC"/static/js/client/sw.js      www/sw.js
# The WHOLE vendor tree, not a hand-picked list. Every one of these is pulled in by a root-relative
# <script src>/<link href>/@font-face, which the fetch shim never sees — the browser resolves them
# against the page, so anything not here 404s against the bundle and the feature that needs it dies
# with no console error worth reading. An enumerated list is how jsqr (QR SCANNING, the "scan a signer
# QR" login) and katex (the maths in Flashcards) were silently missing from the app for as long as the
# app has existed: both are loaded on DEMAND, so nothing breaks until someone opens that one screen.
# Copying the directory means adding a vendored library cannot forget the app again.
cp -r "$SRC"/static/vendor            www/static/
cp "$SRC"/static/*.png                www/static/ 2>/dev/null || true

PC_VER="$(date -u +%s)" python3 - "$SRC" <<'PY'
import os, re, sys

src = sys.argv[1]
ver = os.environ.get('PC_VER', '0')
html = open(os.path.join(src, 'templates', 'client.html'), encoding='utf-8').read()

# ---- render the four template values ------------------------------------------------------------
# `nostr_only` is FALSE here on purpose: false is the branch that KEEPS every nav item in the markup,
# which is what lets applyInstanceGating() hide or restore them per instance at runtime. Baking true
# would delete the buttons from the bundle and no instance could ever bring them back.
html = re.sub(r'\{%\s*if not nostr_only\s*%\}(.*?)\{%\s*endif\s*%\}', r'\1', html, flags=re.S)
html = re.sub(r'\{%\s*if nostr_only\s*%\}.*?\{%\s*endif\s*%\}', '', html, flags=re.S)
# `secure` gates the upgrade-insecure-requests CSP. Always drop it: the page is served from a secure
# app:// origin, but the INSTANCE may legitimately be cleartext (an .onion is plain HTTP by design, and
# so is a LAN box), and that CSP would silently rewrite every fetch/WebSocket/<img> to https://<host>,
# which does not exist. Same reasoning as the APK's strip, which learned it the hard way.
html = re.sub(r'\{%\s*if secure\s*%\}.*?\{%\s*endif\s*%\}', '', html, flags=re.S)
html = html.replace('{{ default_theme|default("cyberpunk") }}', 'cyberpunk')
html = html.replace('{{ ver }}', ver)
html = html.replace("{{ 'true' if nostr_only else 'false' }}", 'false')
left = re.search(r'\{\{.*?\}\}|\{%.*?%\}', html, flags=re.S)
if left:
    raise SystemExit('build-www: unrendered template tag in client.html: ' + left.group(0)[:80])

# manifest is a PWA concept — it 404s against the bundle and this is a native app, not an install target
html = re.sub(r'<link[^>]+rel=["\']manifest["\'][^>]*>', '', html)

shim = '''<script>
/* Bundled mode for the desktop app.
 *
 * The instance is whatever the user chose, or NOTHING. Both are first-class: with no instance the app is
 * a Nostr client (relays + key), and every server-backed surface is hidden by applyInstanceGating()
 * rather than left to fail. __PC_API_BASE__ is therefore always DEFINED (that is how app.js knows it is
 * bundled) and may be an empty string (that is how it knows there is no instance).
 *
 * The shell owns the stored value, not localStorage: the main process needs it too — for the tor proxy
 * decision, for the off-site link rule, and to survive a cache clear — so config.json is the one copy
 * and this reads the snapshot the preload handed us. */
window.__PC_API_BASE__ = (function(){
  try { var s = (window.pcShell && window.pcShell.instanceSync) || ''; return String(s).replace(/\\/+$/, ''); }
  catch(e) { return ''; }
})();
window.__PC_SET_INSTANCE__ = function(u){
  try { if (window.pcShell && window.pcShell.setInstance) { window.pcShell.setInstance(u || ''); return; } } catch(e){}
  try { location.reload(); } catch(e){}
};
// Bearer token for the instance, set by app.js (_setAiToken) once nostr-login returns one, and read
// per-request by _auth below so a token acquired mid-session applies immediately. Cookies are not usable
// against a .onion instance (plain http, and SameSite=None demands Secure), so the header is the only
// auth that works there. In-memory only: a persisted token would outlive a change of key or instance.
window.__PC_TOKEN__ = '';
(function(){
  var B = window.__PC_API_BASE__, W = B.replace(/^http/, 'ws');
  var _f = window.fetch.bind(window);
  function _auth(o){
    try{
      var t = window.__PC_TOKEN__; if(!t) return o;
      var h = o.headers;
      // NEVER clobber an Authorization the caller already set — Blossom and NIP-98 uploads carry their
      // own `Nostr <base64>` header and would break.
      if (h && typeof h.get === 'function'){ if(!h.get('Authorization')) h.set('Authorization', 'Bearer '+t); return o; }
      h = Object.assign({}, h || {});
      var has = false; for (var k in h){ if (String(k).toLowerCase() === 'authorization') has = true; }
      if (!has) h['Authorization'] = 'Bearer '+t;
      o.headers = h; return o;
    }catch(e){ return o; }
  }
  // Not every root-relative URL is a server call: the bundle serves its OWN assets at exactly those
  // paths (/static/..., /sw.js, /index.html), and they must be fetched from the bundle whether or not an
  // instance exists. Rewriting them would send the app's own stylesheet and fonts to a remote server,
  // and rejecting them broke @font-face and the sprite outright.
  function isLocal(p){ return p.indexOf('/static/') === 0 || p === '/sw.js' || p === '/index.html'; }
  // With no instance, a root-relative API path has no server to resolve against. Reject rather than let
  // it resolve to app://posterchan/... : that would be a request to the bundle itself, which answers 404
  // for /api/*, and "no instance" would look like "broken instance". A rejected promise is the same
  // shape as the network failure every one of these call sites already catches.
  var NO_SERVER = 'PosterChan: no instance configured (running on relays only)';
  window.fetch = function(i, o){
    try {
      var p = (typeof i === 'string') ? i : (i && i.url) || '';
      if (p.charAt(0) === '/' && !isLocal(p)){
        if (!B) return Promise.reject(new TypeError(NO_SERVER));
        // credentials:'include' — these are cross-origin (app://posterchan -> the instance), so without
        // it the session cookie is neither sent nor stored and every authed call 401-loops. Paired with
        // the server's SameSite=None cookie + CORS allow-credentials for this origin.
        if (typeof i === 'string'){ i = B + i; o = _auth(Object.assign({}, o, {credentials:'include'})); }
        else { o = _auth(Object.assign({}, o, {credentials:'include'})); i = new Request(B + i.url, i); }
      }
    } catch(e){}
    return _f(i, o);
  };
  var _WS = window.WebSocket;
  window.WebSocket = function(u, p){
    if (typeof u === 'string' && u.charAt(0) === '/'){
      if (!W) throw new Error(NO_SERVER);
      u = W + u;
    }
    return new _WS(u, p);
  };
  window.WebSocket.prototype = _WS.prototype;
  // Marks the native app so CSS can reserve chrome the way the APK does. Desktop has no system status
  // bar to dodge, but the class is what the shared stylesheet keys native spacing off.
  document.addEventListener('DOMContentLoaded', function(){ try{ document.body.classList.add('native','desktop'); }catch(e){} });
})();
</script>
'''
html = html.replace('</head>', shim + '</head>', 1)
open('www/index.html', 'w', encoding='utf-8').write(html)
print('www/index.html built (shell rendered locally, bundled mode injected)')
PY

echo "www assembled: $(find www -type f | wc -l) files"

# electron-builder's extraResources entry points at resources/tor, and a MISSING `from` directory fails
# the pack outright — so a developer who has not downloaded the Tor Expert Bundle could not build at all.
# Create it empty instead: tor.js finds no binary, reports "This build does not include Tor" and the rest
# of the app works. The RELEASE gate is not here, it is the workflow's "Bundle Tor" step, which downloads
# the bundle and hard-fails if either the binary or the geoip database is absent.
if [ ! -d resources/tor ]; then
  mkdir -p resources/tor
  cat > resources/tor/README.txt <<'EOT'
Empty on purpose: this is where the Tor Expert Bundle is extracted.

CI (.github/workflows/desktop.yml, the "Bundle Tor" step) downloads it per platform and fails the build
if the tor binary or the geoip database is missing, so released installers always carry Tor. A LOCAL
build without it still runs — the Tor panel reports that this build does not include Tor.

To populate it by hand, extract tor-expert-bundle-<platform>-<version>.tar.gz here, so that you get:
  resources/tor/tor/tor(.exe)
  resources/tor/data/geoip
  resources/tor/data/geoip6
EOT
  echo "resources/tor: created empty (no Tor bundled — see resources/tor/README.txt)"
fi
