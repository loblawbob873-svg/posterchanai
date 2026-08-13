"""An AI-chat download must SAVE the bytes in the APK, not just say it did.

The agent's `/workspace` backup reaches the user as `[⬇️ sandbox-workspace.tar.gz](/api/files/…)`,
which `aiFormat` turns into a `.ai-dlfile` button (tests/client/test_ai_artifact_link.py). That fixed
the half where the bundled shell never even asked the server for the file. The OTHER half stayed
broken and looked exactly like success: the handler fetched the bytes, built an object URL, clicked a
programmatic `<a download>` — which the Android WebView IGNORES, because MainActivity registers no
DownloadListener — and then wrote "✓ downloaded" on the button. Nothing threw, nothing logged, and
the one artifact a user has no other way to get at was never written anywhere. Reported as "click
download for archive, says downloaded".

The route that DOES work on-device already existed and is what every media save uses: `saveBlobAs`,
which writes the blob into the cache directory and hands it to the OS share sheet (Save to Files /
Send…). The sheet appearing is the confirmation, so the button says SHARED there — claiming "saved"
would be the same lie one step later, since the user has not picked a destination yet.

  native-saves-the-bytes   on a Capacitor shell the blob reaches Filesystem.writeFile + Share.share,
                           under the artifact's own filename, and NO <a download> is clicked
  native-says-shared       the button reports the share sheet, never "✓ downloaded"
  web-still-downloads      in a browser it is still an <a download> with the right name, and still
                           says "✓ downloaded" — the fix must not cost the working platform
  base64-media-too         the effect/geni row (bytes already in the message, no fetch) takes the
                           same route; it had the identical bare <a download>
  failure-restores-label   a failed download puts back the label it found — that label is the
                           FILENAME on the artifact button, not the word "Download"
  check-can-fail           with saveBlobAs replaced by the pre-fix anchor click, the native run
                           writes no file and still says "✓ downloaded" — so a pass here means the
                           handler, not the harness

The handlers are extracted from app.js rather than copied, so they cannot drift from what ships.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(REPO, "static", "js", "client", "app.js")

ARTIFACT = "/api/files/verita84%40poster.place/1336/enc_" + "d" * 64 + ".gz"


def _fn(src, name, opener):
    """Pull one top-level function out of app.js by brace counting from its opening line."""
    i = src.index(opener)
    depth, j, started = 0, i, False
    while j < len(src):
        if src[j] == "{":
            depth += 1
            started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                return src[i:j + 1]
        j += 1
    raise AssertionError("could not bound " + name)


# The shell around the handlers: a recording <a>.click, a fetch that answers with real bytes, and a
# Capacitor that is present only for the native runs.
STUBS = r"""
const log = { writes:[], shares:[], clicks:[], toasts:[] };
const toast = m => log.toasts.push(String(m));
const _ai = { fxMedia: { fx1: { b64: btoa('song-bytes'), mime:'audio/mpeg', ext:'mp3' } } };
// Never let a click actually navigate/download in the harness — record it, which is also the only
// way to see the pre-fix behaviour at all (a WebView shows nothing either way).
HTMLAnchorElement.prototype.click = function(){ log.clicks.push({ href:this.href, download:this.download }); };
window.fetch = async (u, opts) => ({ ok:true, status:200, url:u,
  blob: async () => new Blob([new Uint8Array([31,139,8,0,0,0,0,0])], { type:'application/gzip' }) });
function nativeShell(){
  window.Capacitor = {
    isNativePlatform: () => true,
    Plugins: {
      Filesystem: { writeFile: async o => { log.writes.push({ path:o.path, directory:o.directory,
                                                              bytes:(o.data||'').length });
                                            return { uri:'file:///cache/'+o.path }; } },
      Share: { share: async o => { log.shares.push({ files:o.files, title:o.dialogTitle }); } },
    },
  };
}
function btn(label){
  const b = document.createElement('button');
  b.innerHTML = '<svg class="ic"></svg>';           // the sprite icon _btnText must not eat
  b.appendChild(document.createTextNode(label));
  document.body.appendChild(b);
  return b;
}
const labelOf = b => [...b.childNodes].filter(n => n.nodeType === 3).map(n => n.nodeValue).join('').trim();
"""

# The handler exactly as it was before the fix, used ONLY by the can-fail run. Kept as an override of
# saveBlobAs so the shipped handler is still the code under test.
PREFIX_SAVE = r"""
async function saveBlobAs(blob, name){
  const u=URL.createObjectURL(blob), a=document.createElement('a');
  a.href=u; a.download=name; document.body.appendChild(a); a.click(); a.remove();
  return 'saved';
}
"""


def _harness(src, *, with_fix=True):
    body = "\n".join([
        STUBS,
        _fn(src, "_isNativeApp", "function _isNativeApp(){"),
        _fn(src, "_blobToB64", "function _blobToB64(blob){"),
        _fn(src, "saveBlobAs", "async function saveBlobAs(blob, name){"),
        _fn(src, "_btnText", "function _btnText(btn, text){"),
        _fn(src, "_btnLabel", "function _btnLabel(btn){"),
        _fn(src, "downloadFileUrl", "async function downloadFileUrl(u, btn, name){"),
        _fn(src, "downloadEffectMedia", "async function downloadEffectMedia(mid, btn){"),
    ])
    if not with_fix:
        body += PREFIX_SAVE
    return body


PAGE = """<!doctype html><meta charset="utf-8"><body><script>
%s
const out = {};
(async () => {
  try {
    %s
    const b = btn('sandbox-workspace.tar.gz');
    await downloadFileUrl(%s, b, 'sandbox-workspace.tar.gz');
    out.label = labelOf(b);
    out.disabled = b.disabled;

    const fb = btn('Download');
    await downloadEffectMedia('fx1', fb);
    out.fxLabel = labelOf(fb);

    // A download that fails must give the button back the label it FOUND.
    window.fetch = async () => { throw new Error('offline'); };
    const eb = btn('sandbox-workspace.tar.gz');
    await downloadFileUrl(%s, eb, 'sandbox-workspace.tar.gz');
    out.failLabel = labelOf(eb);
    out.failDisabled = eb.disabled;
    out.failIcon = !!eb.querySelector('svg');

    out.writes = log.writes; out.shares = log.shares; out.clicks = log.clicks; out.toasts = log.toasts;
  } catch (e) { out.error = String((e && e.stack) || e); }
  document.title = JSON.stringify(out);
})();
</script>"""


CHROME = (shutil.which("google-chrome-stable") or shutil.which("chromium")
          or shutil.which("chrome") or shutil.which("google-chrome"))


def _run(harness, *, native):
    """Render the page and read the result back out of <title>.

    No `--remote-debugging-port` and a throwaway profile per call: the suite runs several checks at
    once and a fixed port/profile is the collision PC_CHECK_PORT exists to avoid."""
    if not CHROME:
        raise unittest.SkipTest("no chrome on this host")
    page_src = PAGE % (harness, "nativeShell();" if native else "",
                       json.dumps(ARTIFACT), json.dumps(ARTIFACT))
    d = tempfile.mkdtemp(prefix="pc-aidl-")
    try:
        page = os.path.join(d, "t.html")
        with open(page, "w") as fh:
            fh.write(page_src)
        out = subprocess.run(
            [CHROME, "--headless", "--disable-gpu", "--no-sandbox", "--virtual-time-budget=4000",
             "--user-data-dir=" + os.path.join(d, "profile"), "--dump-dom", "file://" + page],
            capture_output=True, text=True, timeout=180).stdout
        m = re.search(r"<title>(.*?)</title>", out, re.S)
        if not m:
            raise AssertionError("the page did not render:\n" + out[:2000])
        got = json.loads(re.sub(r"&quot;", '"', m.group(1)))
        assert "error" not in got, got.get("error")
        return got
    finally:
        shutil.rmtree(d, ignore_errors=True)


class AiDownloadNative(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(APP) as fh:
            cls.src = fh.read()
        cls.harness = _harness(cls.src)

    def test_native_saves_the_bytes(self):
        o = _run(self.harness, native=True)
        paths = [w["path"] for w in o["writes"]]
        self.assertIn("sandbox-workspace.tar.gz", paths,
                      "the archive never reached the filesystem plugin: " + repr(o["writes"]))
        self.assertTrue(o["writes"][0]["bytes"] > 0, "an empty file was written")
        self.assertTrue(o["shares"], "nothing was handed to the share sheet — on-device that IS the save")
        self.assertEqual(o["clicks"], [],
                         "a programmatic <a download> was clicked in the APK, which the WebView "
                         "ignores: " + repr(o["clicks"]))

    def test_native_says_shared(self):
        o = _run(self.harness, native=True)
        self.assertEqual(o["label"], "✓ shared",
                         "the button claimed a save the user has not made yet: " + repr(o["label"]))
        self.assertFalse(o["disabled"], "the button stayed disabled after a successful save")

    def test_web_still_downloads(self):
        o = _run(self.harness, native=False)
        self.assertEqual(o["writes"], [], "the browser path went through the native plugins")
        names = [c["download"] for c in o["clicks"]]
        self.assertEqual(len(names), 2, "one of the two saves never reached the disk: " + repr(names))
        self.assertEqual(names[0], "sandbox-workspace.tar.gz",
                         "the web download lost its filename: " + repr(names))
        self.assertRegex(names[1], r"^posterchan-\d+\.mp3$", repr(names))
        self.assertEqual(o["label"], "✓ downloaded")

    def test_base64_media_too(self):
        """The effect/geni row holds its bytes in the message — same route, same report."""
        o = _run(self.harness, native=True)
        self.assertEqual(o["fxLabel"], "✓ shared", repr(o["fxLabel"]))
        self.assertTrue(any(w["path"].endswith(".mp3") for w in o["writes"]),
                        "the base64 media path never wrote a file: " + repr(o["writes"]))
        self.assertEqual(len(o["shares"]), 2, "one of the two saves skipped the share sheet")

    def test_failure_restores_label(self):
        o = _run(self.harness, native=True)
        self.assertEqual(o["failLabel"], "sandbox-workspace.tar.gz",
                         "a failed download renamed the artifact button: " + repr(o["failLabel"]))
        self.assertFalse(o["failDisabled"], "a failed download left the button disabled for ever")
        self.assertTrue(o["failIcon"], "the failure path ate the button's sprite icon")
        self.assertTrue(any("download failed" in t for t in o["toasts"]),
                        "the failure was silent: " + repr(o["toasts"]))

    def test_check_can_fail(self):
        """With the pre-fix saver, the native run writes nothing and still reports success."""
        o = _run(_harness(self.src, with_fix=False), native=True)
        self.assertEqual(o["writes"], [])
        self.assertEqual(o["shares"], [])
        self.assertTrue(o["clicks"], "the pre-fix shape did not even click an anchor")
        self.assertEqual(o["label"], "✓ downloaded",
                         "removing the fix did not reproduce the bug — the harness is not "
                         "exercising the path this test claims to cover")


if __name__ == "__main__":
    unittest.main()
