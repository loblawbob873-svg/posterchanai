"""A PNG posted to Social is held to the same size cap as a photo.

Run: venv-unified/bin/python -m unittest tests.client.test_png_uploads_are_compressed
     (needs google-chrome-stable — canvas encoding is the thing under test; skips without it)

THE BUG — a 2560x1529 desktop screenshot uploaded through the composer landed on Blossom at 5.9 MB,
byte-for-byte the file that was picked, and GitHub's image proxy (5 MB limit) refused to show it.

compressImage keeps a PNG a PNG (so a transparent logo is not flattened onto black), and a PNG has
no quality knob. So the only thing that could ever shrink one was the 2560px downscale — and a
screenshot is usually AT or UNDER 2560px. The lossless re-encode came out no smaller, "never make it
bigger" handed back the original, and the 800 KB cap was, in practice, enforced for JPEG only.

Measured with the shipped code on the real screenshots: 5,938,804 -> 5,938,804 and
2,081,043 -> 2,081,043 before; 406,510 and 404,210 (WebP) after.

The rules this pins, each measured on real canvas output rather than read off the source:

  shrinks      an opaque PNG over the cap comes back under it
  alpha        a TRANSPARENT one comes back smaller and still transparent (WebP keeps alpha)
  small        a PNG already under the cap is returned untouched — same File object
  no WebP      a browser whose toBlob cannot encode WebP (it silently returns PNG): an opaque PNG
               falls back to JPEG, a transparent one stays lossless PNG — never flattened
  JPEG         photos still come out JPEG
  AI chat      NOT the above. AI chat sends images for OCR/read-text and only ever runs a gentle
               >8 MB safety reduce; a PNG there must stay LOSSLESS PNG exactly as before. The PNG ->
               WebP step is opt-in (`lossyPng`) and only compressMedia (the Blossom/Social upload)
               opts in. The AI case runs the OPTIONS LITERAL READ OUT OF THE SHIPPED ai.js, so a
               later edit that opts AI chat in fails here.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPLOAD = os.path.join(REPO, "static", "js", "client", "upload.js")
AIJS = os.path.join(REPO, "static", "js", "client", "ai.js")
CHROME = shutil.which("google-chrome-stable") or shutil.which("chromium") or shutil.which("chrome")
CAP = 800 * 1024

PAGE = r"""<!doctype html><meta charset="utf-8">
<script src="upload.js"></script>
<script>
// A screenshot-shaped picture: flat panels and text (which PNG compresses well) over a textured
// wallpaper (which it does not) — enough to put a lossless PNG well over the cap at 2560px.
function paint(w, h, alpha, noisy){
  const c = document.createElement('canvas'); c.width = w; c.height = h;
  const x = c.getContext('2d'), img = x.createImageData(w, h), d = img.data;
  let s = 7; const rnd = () => (s = (s * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
  for (let y = 0; y < h; y++) for (let i = 0; i < w; i++) {
    const k = (y * w + i) * 4;
    if (noisy) { d[k] = 255 * rnd(); d[k+1] = 255 * rnd(); d[k+2] = 255 * rnd(); }
    else { d[k] = 40 + 120 * i / w + 30 * rnd(); d[k+1] = 30 + 90 * y / h + 30 * rnd(); d[k+2] = 90 + 40 * rnd(); }
    d[k+3] = alpha && i < w / 4 ? 0 : 255;          // a fully transparent strip down the left edge
  }
  x.putImageData(img, 0, 0);
  x.fillStyle = 'rgba(12,14,24,.92)'; x.fillRect(w * .3, h * .1, w * .4, h * .7);
  x.fillStyle = '#9ef'; x.font = '28px sans-serif';
  for (let r = 0; r < 20; r++) x.fillText('Notifications  Social  Messages  Files ' + r, w * .32, h * .15 + r * 36);
  return c;
}
const asFile = (c, type, name) => new Promise(r => c.toBlob(b => r(new File([b], name, {type})), type, 0.95));
async function alphaAt(file, px, py){
  const bmp = await createImageBitmap(file), c = document.createElement('canvas');
  c.width = bmp.width; c.height = bmp.height; const x = c.getContext('2d'); x.drawImage(bmp, 0, 0);
  return x.getImageData(px, py, 1, 1).data[3];
}
async function probe(m, file, opts){
  const out = await m.compressImage(file, opts);
  return {inSize: file.size, inType: file.type, size: out.size, type: out.type, name: out.name,
          same: out === file, alphaLeft: await alphaAt(out, 5, 5), alphaMid: await alphaAt(out, 1280, 700)};
}
window.__report = (async () => {
  const report = {};
  try {
    const m = window.PCUploadFactory(new Proxy({state: {}}, {get: (t, k) => k in t ? t[k] : undefined}));
    const opaque = await asFile(paint(2560, 1440, false), 'image/png', 'desktop.png');
    const clear  = await asFile(paint(2560, 1440, true),  'image/png', 'logo.png');
    const small  = await asFile(paint(400, 300, false),   'image/png', 'small.png');
    const photo  = await asFile(paint(2560, 1440, false), 'image/jpeg', 'photo.jpg');
    const SOCIAL = {lossyPng: true};                 // what compressMedia (uploadBlob) passes
    report.opaque = await probe(m, opaque, SOCIAL);
    report.clear  = await probe(m, clear,  SOCIAL);
    report.small  = await probe(m, small,  SOCIAL);
    report.photo  = await probe(m, photo,  SOCIAL);
    // AI chat: a PNG over its 8 MB threshold, reduced with the options ai.js ships.
    const big = await asFile(paint(2560, 1440, false, true), 'image/png', 'scan.png');
    report.aiPng = await probe(m, big, /*AI_OPTS*/);
    // A browser that cannot ENCODE WebP (older Safari) returns a PNG from toBlob('image/webp').
    const real = HTMLCanvasElement.prototype.toBlob;
    HTMLCanvasElement.prototype.toBlob = function(cb, type, q){
      return real.call(this, cb, type === 'image/webp' ? 'image/png' : type, q);
    };
    report.noWebpOpaque = await probe(m, opaque, SOCIAL);
    report.noWebpClear  = await probe(m, clear,  SOCIAL);
    HTMLCanvasElement.prototype.toBlob = real;
  } catch (e) { report.error = String(e && e.stack || e); }
  return JSON.stringify(report);
})();
</script>
"""

_CACHE = {}


def _run(upload_js):
    """Load the page in a real Chrome and await its report over CDP.

    Not --dump-dom: that returns when VIRTUAL time runs out, and canvas encoding happens on real
    threads — the dump came back before a single toBlob had resolved. Port 0 + DevToolsActivePort,
    so parallel shards cannot collide on a port."""
    if upload_js in _CACHE:
        return _CACHE[upload_js]
    import asyncio
    import time
    import urllib.request
    import websockets

    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "upload.js"), "w", encoding="utf-8") as fh:
            fh.write(upload_js)
        page = os.path.join(tmp, "probe.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(PAGE.replace("/*AI_OPTS*/", _ai_opts()))
        prof = os.path.join(tmp, "profile")
        chrome = subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--remote-debugging-port=0",
             f"--user-data-dir={prof}", "file://" + page],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            port_file, deadline = os.path.join(prof, "DevToolsActivePort"), time.time() + 30
            while not os.path.exists(port_file) and time.time() < deadline:
                time.sleep(0.1)
            port = open(port_file).read().split()[0]
            ws_url = None
            while not ws_url and time.time() < deadline:
                try:
                    pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json"))
                    ws_url = next((p["webSocketDebuggerUrl"] for p in pages
                                   if p["type"] == "page" and p["url"].endswith("probe.html")), None)
                except OSError:
                    pass
                if not ws_url:
                    time.sleep(0.1)
            assert ws_url, "Chrome never opened the probe page"

            async def ask():
                async with websockets.connect(ws_url, max_size=1 << 24) as ws:
                    for _ in range(100):      # the page script may not have run yet
                        await ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
                            "expression": "window.__report ? window.__report : null",
                            "awaitPromise": True, "returnByValue": True}}))
                        while True:
                            r = json.loads(await ws.recv())
                            if r.get("id") == 1:
                                break
                        v = r.get("result", {}).get("result", {}).get("value")
                        if v:
                            # Close it properly: a killed Chrome leaves helpers still writing into
                            # the profile while TemporaryDirectory is deleting it.
                            await ws.send(json.dumps({"id": 2, "method": "Browser.close"}))
                            return v
                        await asyncio.sleep(0.1)
                    raise AssertionError(f"the page never reported: {r}")

            got = json.loads(asyncio.run(asyncio.wait_for(ask(), 150)))
        finally:
            try:
                chrome.wait(timeout=15)
            except subprocess.TimeoutExpired:
                chrome.kill()
                chrome.wait()
        assert "error" not in got, got["error"]
        _CACHE[upload_js] = got
        return got


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _ai_opts():
    """The options object AI chat passes to compressImage, as shipped."""
    calls = re.findall(r"compressImage\(f, (\{[^}]*\})\)", _read(AIJS))
    assert len(calls) == 1, f"expected ONE compressImage call in ai.js's attach path, found {calls} — re-point this"
    return calls[0]


def _shipped():
    with open(UPLOAD, encoding="utf-8") as fh:
        return _run(fh.read())


@unittest.skipUnless(CHROME, "needs Chrome to encode real canvases")
class PngUploadsAreCompressed(unittest.TestCase):
    def test_the_fixture_is_the_bug_shape(self):
        """Otherwise every assertion below could pass on a PNG that was never over the cap."""
        got = _shipped()
        for k in ("opaque", "clear"):
            self.assertGreater(got[k]["inSize"], CAP, f"{k} fixture is not over the cap: {got[k]}")

    def test_an_opaque_screenshot_comes_back_under_the_cap(self):
        got = _shipped()["opaque"]
        self.assertFalse(got["same"], f"a {got['inSize']:,}-byte PNG screenshot went up untouched")
        self.assertLessEqual(got["size"], CAP, got)
        self.assertEqual(got["type"], "image/webp", got)
        self.assertTrue(got["name"].endswith(".webp"), f"name and type disagree: {got}")

    def test_a_transparent_png_shrinks_and_stays_transparent(self):
        got = _shipped()["clear"]
        self.assertLess(got["size"], got["inSize"], got)
        self.assertEqual(got["alphaLeft"], 0, f"the transparent strip came back opaque: {got}")
        self.assertEqual(got["alphaMid"], 255, got)

    def test_a_small_png_is_left_alone(self):
        got = _shipped()["small"]
        self.assertLessEqual(got["inSize"], CAP, "fixture: the small PNG must be under the cap")
        self.assertTrue(got["same"], f"a PNG under the cap was re-encoded: {got}")

    def test_a_photo_is_still_jpeg(self):
        got = _shipped()["photo"]
        self.assertEqual(got["type"], "image/jpeg", got)
        self.assertLessEqual(got["size"], CAP, got)

    def test_without_webp_an_opaque_png_becomes_jpeg(self):
        got = _shipped()["noWebpOpaque"]
        self.assertEqual(got["type"], "image/jpeg", got)
        self.assertLessEqual(got["size"], CAP, got)
        self.assertTrue(got["name"].endswith(".jpg"), got)

    def test_ai_chat_keeps_a_png_lossless(self):
        """The AI chat attach path must behave exactly as before: a PNG stays PNG (OCR needs it)."""
        got = _shipped()["aiPng"]
        self.assertGreater(got["inSize"], 8 * 1024 * 1024,
                           f"fixture: must be over AI chat's 8 MB threshold to reach compressImage: {got}")
        self.assertEqual(got["type"], "image/png",
                         f"AI chat's reduce ({_ai_opts()}) turned a PNG lossy: {got}")

    def test_ai_chat_does_not_opt_in(self):
        self.assertNotIn("lossyPng", _ai_opts(), "AI chat must never opt into the lossy PNG step")

    def test_the_social_upload_opts_in(self):
        """compressMedia is what uploadBlob runs; without the flag the fix above reaches nobody."""
        body = re.search(r"async function compressMedia\(file\)\{(.*?)\n  \}", _read(UPLOAD), re.S)
        self.assertTrue(body, "compressMedia moved — re-point this")
        self.assertRegex(body.group(1), r"compressImage\(file,\s*\{[^}]*lossyPng:\s*true",
                         "uploadBlob's compressMedia must pass {lossyPng:true}")

    def test_without_webp_a_transparent_png_is_never_flattened(self):
        """JPEG has no alpha: the fallback would paint the transparent strip black."""
        got = _shipped()["noWebpClear"]
        self.assertNotEqual(got["type"], "image/jpeg", f"a transparent PNG was flattened to JPEG: {got}")
        self.assertEqual(got["alphaLeft"], 0, got)


if __name__ == "__main__":
    unittest.main()
