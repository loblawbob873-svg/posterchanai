"""What the service worker does and does NOT intercept, and into WHICH cache (static/js/client/sw.js).

Run: venv-unified/bin/python -m unittest tests.test_sw_video_routing

Two routing decisions live here. The second (EncryptedDriveBlobs) is the encrypted drive: blobs read
with fetch(), whose request.destination is '', which no rule in the worker matched.

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
const opened = [];   // which cache a branch reaches for -- that IS the routing decision for reads
const stored = [];   // and what it actually PUT there, which is the decision that outlives the tab
const CACHES_FAIL = %s;
globalThis.caches = { open: async (name) => { opened.push(name);
                        if (CACHES_FAIL) throw new DOMException('The operation failed for an operation-specific reason', 'UnknownError');
                        return { match: async () => undefined,
                                 put: async (rq) => { stored.push([name, rq && rq.url]); },
                                 keys: async () => [], delete: async () => {} }; },
                      match: async () => undefined, keys: async () => [], delete: async () => {} };
// The response the network "returns". Opaque by default (what a cross-origin <video> really gets, and
// benign for branches that fetch asynchronously: a throwing stub would surface as an unhandled
// rejection that kills node AFTER the routing decision was already made). RES overrides it where the
// question is what gets STORED, which depends on the status, content-type and length.
const RES = %s;
// A response the worker can actually READ: cacheFirstBlob buffers the body rather than teeing it,
// so a stub with only headers no longer exercises the path it is meant to.
globalThis.Headers = globalThis.Headers || class { constructor(h){ this._h = h || {}; }
                                                   get(k){ return this._h.get ? this._h.get(k) : null; } };
globalThis.fetch = async () => ({ status: RES.status, type: RES.status ? 'basic' : 'opaque',
                                  ok: RES.status === 200, clone(){ return this; },
                                  arrayBuffer: async () => new ArrayBuffer(8),
                                  headers: { get: (k) => RES[String(k).toLowerCase()] || null } });
globalThis.Response = class { static error(){ return {}; } constructor(body, init){ this.body = body; this.init = init; } };
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
  let served = 'none';
  const realRespond = e.respondWith;
  e.respondWith = (p) => { intercepted = true;
    Promise.resolve(p).then(r => { served = (r && r.status === 200) ? 'ok'
                                          : (r && r.body !== undefined) ? 'response' : 'other'; },
                            () => { served = 'REJECTED'; }); };
  try { handler(e); } catch (err) { console.log(JSON.stringify({error: String(err)})); }
  // A put happens several awaits after the handler returns, so let the microtasks drain first.
  setTimeout(() => console.log(JSON.stringify({ intercepted, opened, stored, served })), 30);
}
"""

WEB_SW = "https://poster.place/static/js/client/sw.js"   # web PWA: /client scope
APP_SW = "https://poster.place/sw.js"                     # bundled desktop/APK: root scope -> IS_APP


OPAQUE = {"status": 0}                                            # what a no-cors fetch really returns
CIPHERTEXT = {"status": 200, "content-type": "application/octet-stream", "content-length": "4096"}
JSON_BODY = {"status": 200, "content-type": "application/json", "content-length": "512"}


def _run(sw_url, url, destination, mode="no-cors", res=None, caches_fail=False):
    with open(SW, encoding="utf-8") as fh:
        src = fh.read()
    # ORDER MATTERS and follows the template, not the signature: PAGE, REQ, CACHES_FAIL, RES, src.
    js = HARNESS % (json.dumps(sw_url),
                    json.dumps({"url": url, "destination": destination, "mode": mode}),
                    "true" if caches_fail else "false",
                    json.dumps(res or OPAQUE),
                    src)
    out = subprocess.run([shutil.which("node") or "node", "-e", js],
                         capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(f"node failed: {out.stderr.strip()[:500]}")
    got = json.loads(out.stdout.strip().splitlines()[-1])
    if "error" in got:
        raise AssertionError(got["error"])
    return got


def _route(sw_url, url, destination, mode="no-cors"):
    return _run(sw_url, url, destination, mode)["intercepted"]


def _cache_used(sw_url, url, destination, mode="cors"):
    """Which cache the request was routed INTO ('' if it wasn't intercepted)."""
    got = _run(sw_url, url, destination, mode)
    return (got.get("opened") or [""])[0] if got["intercepted"] else ""


def _stored_in(sw_url, url, destination, res, mode="cors"):
    """Which cache the response was actually WRITTEN to ('' if it wasn't kept)."""
    got = _run(sw_url, url, destination, mode, res)
    return (got.get("stored") or [["", ""]])[0][0]


TWIMG = ("https://video.twimg.com/amplify_video/2083577909483122688/vid/avc1/"
         "1152x720/vSHDzUpz_MvQKuVC.mp4?tag=14")
OWN_VIDEO = "https://poster.place/blossom/abc123.mp4"
OWN_IMAGE = "https://poster.place/blossom/photo.jpg"          # same-origin media (cacheFirstMedia)
OWN_ICON = "https://poster.place/static/icon-192.png"        # same-origin icon (its own cacheFirst branch)
FEDI_AVATAR = "https://detroitriotcity.com/media/avatar.png"
# The Meme Builder's voice-over layers: a talk clip's speech, served from the media host, which is a
# DIFFERENT ORIGIN from the page even on our own deployment.
XORIGIN_AUDIO = "https://media.poster.place/b7bee9fd9452f45fe6e60e7c0e0685954a9aff81e63.wav"
OWN_AUDIO = "https://poster.place/blossom/voice.wav"


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

    def test_a_cross_origin_audio_is_not_intercepted(self):
        """The same bug as video, in the branch that was missed.

        <audio> without a crossorigin attribute fetches no-cors too, so proxying it hands back an
        OPAQUE body that cannot satisfy a Range request. Audio had no rule of its own, so it fell to
        the catch-all `fetch(e.request)` at the bottom — the same proxy, the same opaque result.
        Firefox reports it as NS_ERROR_DOM_MEDIA_DECODE_ERR / "FFmpeg audio error" rather than as an
        unsupported format, which is why it read as a broken FILE rather than as this.

        Found on the Meme Builder's voice-over layers: the export had sound and the preview did not.
        The .wav were canonical PCM (RIFF/WAVE, fmt 16, format 1, mono 24kHz/16-bit, data chunk exactly
        matching the file length) and ffmpeg decoded them clean — nothing was wrong with the bytes."""
        self.assertFalse(_route(WEB_SW, XORIGIN_AUDIO, "audio"))

    def test_a_same_origin_audio_is_left_to_the_catch_all(self):
        """Same-origin audio comes back transparent, so it is range-able and the catch-all's proxy is
        harmless. Pinned so 'fixing' audio does not accidentally send our own files to the network
        without the offline fallback the catch-all provides."""
        self.assertTrue(_route(WEB_SW, OWN_AUDIO, "audio"))

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

    def test_audio_is_never_intercepted_in_the_app(self):
        """The app branch returns early for everything but cross-origin images, so audio was already
        safe there — which is why the voice played in the desktop app and not in Firefox, the same
        split as the video bug."""
        self.assertFalse(_route(APP_SW, XORIGIN_AUDIO, "audio"))
        self.assertFalse(_route(APP_SW, OWN_AUDIO, "audio"))

    def test_the_app_still_caches_cross_origin_images(self):
        self.assertTrue(_route(APP_SW, FEDI_AVATAR, "image"))

    def test_the_app_leaves_its_own_bundle_alone(self):
        """Bundle assets must refresh on an APK update, so the SW must never serve them."""
        self.assertFalse(_route(APP_SW, OWN_IMAGE, "image"))
        self.assertFalse(_route(APP_SW, OWN_ICON, "image"))


SHA = "9f3b" + "a" * 60                                          # a 64-hex content address
OWN_BLOB = f"https://poster.place/blossom/{SHA}"                 # this node's mount path
CUSTOM_BLOB = f"https://blossom.example.com/{SHA}"               # a user's OWN Blossom server
BLOB_THUMB = f"https://poster.place/blossom/{SHA}.png?thumb=1"   # public grid thumbnail (an <img>)


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class EncryptedDriveBlobs(unittest.TestCase):
    """Notes attachments, music tracks and the files index are content-addressed ciphertext read
    with fetch() -- request.destination is '', because the <img> only ever sees the decrypted
    object: URL. That made them invisible to every rule in the worker: they fell through to the
    pass-through branch and were NEVER stored, so opening a note re-downloaded every picture in it
    and "open this note once while online" bought nothing at all."""

    def test_a_drive_blob_is_cached(self):
        self.assertEqual(_cache_used(WEB_SW, OWN_BLOB, ""), "pc-drive-v1")

    def test_a_blob_on_the_users_own_server_is_cached_too(self):
        """encFileUrl fetches mediaServer() + '/' + sha, and mediaServer() is the user's own server
        root whenever they have set one -- so a rule anchored to /blossom/ would have matched
        nothing for exactly those users, silently, with no way for them to tell."""
        self.assertEqual(_cache_used(WEB_SW, CUSTOM_BLOB, ""), "pc-drive-v1")

    def test_the_drive_does_not_share_the_media_cache(self):
        """Separate caches on purpose: the timeline's images are a firehose that would evict a
        deliberately imported note library within a session."""
        self.assertEqual(_cache_used(WEB_SW, BLOB_THUMB, "image"), "pc-media-v2")

    def test_a_navigation_to_a_hash_route_is_not_pinned(self):
        """Cache-first on a navigation would pin a page forever. Only non-navigations qualify."""
        self.assertNotEqual(_cache_used(WEB_SW, OWN_BLOB, "", mode="navigate"), "pc-drive-v1")

    def test_the_bundled_app_caches_the_drive_as_well(self):
        """The APK's worker is media-only so it can never pin stale app code -- but a blob addressed
        by the hash of its own bytes is DATA, and in the bundle it is cross-origin, so nothing else
        would ever store it. Without this, Notes is unusable offline in the app."""
        self.assertEqual(_cache_used(APP_SW, OWN_BLOB, ""), "pc-drive-v1")

    def test_the_bundled_app_still_leaves_app_code_alone(self):
        self.assertFalse(_route(APP_SW, "https://poster.place/static/js/client/app.js", "script"))

    def test_the_ciphertext_is_what_gets_kept(self):
        """Stored by a SEPARATE background fetch — the page's response is the untouched original, so
        `stored` here proves the copy happened, not that the read was routed through it."""
        self.assertEqual(_stored_in(WEB_SW, OWN_BLOB, "", CIPHERTEXT), "pc-drive-v1")

    def test_the_read_is_never_the_thing_this_cache_built(self):
        """The invariant after two outages: whatever the cache does, the page gets the original
        fetch Response. A storage failure, a wrong header, a bad clone — none of them can reach it,
        because the read does not pass through the cache on a miss."""
        with open(SW, encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("async function cacheFirstBlob"):src.index("async function _pumpBlobCache")]
        # The miss path returns `res` itself. No Response is constructed on it, and nothing derived
        # from the body is handed back.
        self.assertIn("return res;", body)
        self.assertNotIn("new Response(", body,
                         "cacheFirstBlob must not build the response the page receives")
        self.assertNotIn("res.clone()", body,
                         "cloning tees the stream the page is reading — that was outage #1")

    def test_a_broken_cache_still_serves_the_file(self):
        """THE invariant, and the one this broke. caches.open()/match() sat outside any try, so once
        the origin's storage came under pressure — which caching gigabytes of attachments is exactly
        how you produce — the rejection escaped respondWith() and EVERY attachment failed. It reached
        the user as "could not open attachment: operation failed for an operation-specific reason",
        i.e. a storage error dressed up as a missing file.

        A cache is an optimisation. Anything it does wrong must cost speed, never the file."""
        got = _run(WEB_SW, OWN_BLOB, "", res=CIPHERTEXT, caches_fail=True)
        self.assertTrue(got["intercepted"])
        self.assertNotEqual(got["served"], "REJECTED",
                            "a storage failure must not fail the read — it must fall through to the network")
        self.assertFalse(got["stored"], "nothing can be stored when the cache is unavailable")

    def test_a_broken_cache_does_not_break_images_either(self):
        got = _run(WEB_SW, OWN_IMAGE, "image", caches_fail=True)
        self.assertNotEqual(got["served"], "REJECTED")

    def test_a_json_listing_is_never_frozen(self):
        """`/blossom/list/<pubkey>` is a LIVE listing of the whole drive, and its path ends in 64 hex
        exactly like a blob's does. Cache-first on that would have pinned someone's file list to
        whatever it was the first time they opened Files — so the hash shape decides the route, and
        the content type decides what is allowed to persist."""
        listing = f"https://poster.place/blossom/list/{SHA}"
        self.assertEqual(_stored_in(WEB_SW, listing, "", JSON_BODY), "")


if __name__ == "__main__":
    unittest.main()
