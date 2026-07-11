#!/bin/bash
# Assemble the Capacitor web bundle (www/) from the SAME web-client files the site serves, then inject
# "bundled mode": every root-relative fetch/WebSocket is rewritten to https://poster.place, so the app
# runs its UI locally (a real app, offline shell) while data (relay, API, Blossom) comes from the server.
# The web UI and the app therefore stay identical — this runs on every deploy (sync.sh / CI).
set -e
cd "$(dirname "$0")"
SRC="$(cd .. && pwd)"

rm -rf www
mkdir -p www/static/js/client www/static/css www/static/vendor/nostr

cp "$SRC"/static/js/client/*.js       www/static/js/client/
cp "$SRC"/static/css/client.css       www/static/css/
cp "$SRC"/static/vendor/nostr/nostr.bundle.js www/static/vendor/nostr/
cp "$SRC"/static/*.png                www/static/ 2>/dev/null || true

# The rendered shell (auth gate + app scaffold) — take the LIVE one so the app matches the site exactly.
curl -fsSL https://poster.place/client -o www/index.html

# Inject bundled-mode (API base + fetch/WS shim) right before </head>, ahead of every app script. Also
# strip cache-busting ?v= from local asset URLs (served from the bundle, not the network) and drop the
# manifest link (native app, not a PWA install).
python3 - <<'PY'
import re
p = 'www/index.html'
html = open(p, encoding='utf-8').read()
shim = '''<script>
window.__PC_API_BASE__ = 'https://poster.place';
(function(){
  var B = window.__PC_API_BASE__, W = B.replace(/^http/, 'ws');
  var _f = window.fetch.bind(window);
  window.fetch = function(i, o){
    try {
      if (typeof i === 'string' && i.charAt(0) === '/') i = B + i;
      else if (i && i.url && i.url.charAt(0) === '/') i = new Request(B + i.url, i);
    } catch(e){}
    return _f(i, o);
  };
  var _WS = window.WebSocket;
  window.WebSocket = function(u, p){ if (typeof u === 'string' && u.charAt(0) === '/') u = W + u; return new _WS(u, p); };
  window.WebSocket.prototype = _WS.prototype;
})();
</script>
'''
html = html.replace('</head>', shim + '</head>', 1)
# local assets are bundled at their /static/... paths — drop the ?v=NNN query so the local server serves them
html = re.sub(r'(/static/[^"\'?\s]+)\?v=[0-9]+', r'\1', html)
# manifest is a PWA concept; harmless but 404s locally — remove it
html = re.sub(r'<link[^>]+rel=["\']manifest["\'][^>]*>', '', html)
open(p, 'w', encoding='utf-8').write(html)
print('www/index.html built (bundled mode injected)')
PY

echo "www assembled: $(find www -type f | wc -l) files"
