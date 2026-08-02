"""What the service worker does and does NOT intercept (static/js/client/sw.js).

Run: venv-unified/bin/python -m unittest tests.test_sw_video_routing

A cross-origin <video> must reach the network untouched.

It has no crossorigin attribute, so it fetches no-cors and fetch() returns an OPAQUE response
(status 0, no headers). Two consequences, measured in Chrome against a real twimg clip:

  * cacheFirstMedia can never store it -- the guard is `res.status === 200`, and opaque is 0. So
    intercepting bought nothing at all.
  * an opaque body cannot satisfy the Range requests a media element makes. Chromium tolerates it;
    FIREFOX fails the load with MEDIA_ERR_SRC_NOT_SUPPORTED, which the user sees as "No video with
    supported format and MIME type found".

That is why a clip played in the Windows desktop app but not in Firefox: the app's SW is root-scoped
(IS_APP) and already skipped video for this exact reason, while the web SW proxied it.

These run the REAL fetch handler against synthetic requests, because the bug is a ROUTING decision --
which branch a request falls into -- and that is invisible in a string assertion.
"""
import json
import os
import shutil
import subprocess
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SW = os.path.join(REPO, "static", "js", "client", "sw.js")

# Enough of the worker globals for sw.js to evaluate and for its fetch handler to run. respondWith
# records the decision instead of producing a response; nothing here touches the network.
HARNESS = r"""
'use strict';
const PAGE = %s;   // the SW's own script URL -> decides IS_APP
const REQ  = %s;   // {url, destination, mode}
let handler = null;
const self = {
  location: new URL(PAGE),
  addEventListener: (name, fn) => { if (name === 'fetch') handler = fn; },
  skipWaiting: () => {}, clients: { claim: () => {}, matchAll: async () => [] },
  registration: { showNotification: () => {} },
};
globalThis.self = self;
globalThis.caches = { open: async () => ({ match: async () => undefined, put: async () => {},
                                           keys: async () => [], delete: async () => {} }),
                      match: async () => undefined, keys: async () => [], delete: async () => {} };
// Benign: some branches fetch asynchronously, and an throwing stub would surface as an unhandled
// rejection that kills node AFTER the routing decision was already made. Routing is what we measure.
globalThis.fetch = async () => ({ status: 0, type: 'opaque', ok: false, clone(){ return this; },
                                  headers: { get: () => null } });
globalThis.Response = class { static error(){ return {}; } constructor(){} };
globalThis.clients = self.clients;
%s
if (!handler) { console.log(JSON.stringify({error: 'no fetch handler registered'})); }
else {
  let intercepted = false;
  const e = {
    request: { url: REQ.url, method: 'GET', destination: REQ.destination, mode: REQ.mode || 'no-cors',
               headers: { get: () => null }, clone(){ return this; } },
    respondWith: () => { intercepted = true; },
    waitUntil: () => {},
  };
  try { handler(e); } catch (err) { console.log(JSON.stringify({error: String(err)})); }
  console.log(JSON.stringify({ intercepted }));
}
"""

WEB_SW = "https://poster.place/static/js/client/sw.js"   # web PWA: /client scope
APP_SW = "https://poster.place/sw.js"                     # bundled desktop/APK: root scope -> IS_APP


def _route(sw_url, url, destination, mode="no-cors"):
    with open(SW, encoding="utf-8") as fh:
        src = fh.read()
    js = HARNESS % (json.dumps(sw_url),
                    json.dumps({"url": url, "destination": destination, "mode": mode}),
                    src)
    out = subprocess.run([shutil.which("node") or "node", "-e", js],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(f"node failed: {out.stderr.strip()[:500]}")
    got = json.loads(out.stdout.strip().splitlines()[-1])
    if "error" in got:
        raise AssertionError(got["error"])
    return got["intercepted"]


TWIMG = ("https://video.twimg.com/amplify_video/2083577909483122688/vid/avc1/"
         "1152x720/vSHDzUpz_MvQKuVC.mp4?tag=14")
OWN_VIDEO = "https://poster.place/blossom/abc123.mp4"
OWN_IMAGE = "https://poster.place/blossom/photo.jpg"          # same-origin media (cacheFirstMedia)
OWN_ICON = "https://poster.place/static/icon-192.png"        # same-origin icon (its own cacheFirst branch)
FEDI_AVATAR = "https://detroitriotcity.com/media/avatar.png"


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class WebServiceWorker(unittest.TestCase):
    def test_a_cross_origin_video_is_not_intercepted(self):
        """THE fix. Proxying it produced an opaque response the media element cannot range-seek,
        which Firefox reports as an unsupported format."""
        self.assertFalse(_route(WEB_SW, TWIMG, "video"))

    def test_a_same_origin_video_is_still_cached(self):
        """Our own uploads come back transparent -- cacheable AND range-able -- and offline replay of
        a small played clip is the feature this cache exists for. Don't throw it away with the fix."""
        self.assertTrue(_route(WEB_SW, OWN_VIDEO, "video"))

    def test_images_are_unaffected(self):
        """Avatars and post images are the main users of the media cache; a cross-origin image gets a
        CORS retry that CAN yield a cacheable 200, which is exactly why video is the special case."""
        self.assertTrue(_route(WEB_SW, FEDI_AVATAR, "image"))
        self.assertTrue(_route(WEB_SW, OWN_IMAGE, "image"))
        self.assertTrue(_route(WEB_SW, OWN_ICON, "image"))


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class BundledAppServiceWorker(unittest.TestCase):
    """Root scope => IS_APP. It was already correct; pin it so the web fix cannot be 'unified' by
    routing app video back through the cache."""

    def test_video_is_never_intercepted_in_the_app(self):
        self.assertFalse(_route(APP_SW, TWIMG, "video"))
        self.assertFalse(_route(APP_SW, OWN_VIDEO, "video"))

    def test_the_app_still_caches_cross_origin_images(self):
        self.assertTrue(_route(APP_SW, FEDI_AVATAR, "image"))

    def test_the_app_leaves_its_own_bundle_alone(self):
        """Bundle assets must refresh on an APK update, so the SW must never serve them."""
        self.assertFalse(_route(APP_SW, OWN_IMAGE, "image"))
        self.assertFalse(_route(APP_SW, OWN_ICON, "image"))


if __name__ == "__main__":
    unittest.main()
