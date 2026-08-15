"""The page must keep offering the signing job until the native service takes it.

WHAT WAS MEASURED, on the relay, while the phone was supposedly signing in the background:

    in 55s:  client REQUESTS=635   phone REPLIES=642

…and the user's report, over and over: "foreground instantly solves the DM problem fast", "putting
it to foreground speeds up the clients decrypting". Both are the same fact — the half answering was
the PAGE, not the service. Chromium throttles a hidden WebView's timers to about one a minute, so a
page-signer runs at full speed on screen and collapses the moment it is not, which is the exact
failure the native service exists to remove.

THE CAUSE IS ONE-SHOT-NESS, not the receipt. `_pushNative()` answers TRUE only when the service
reports a relay socket actually held, which is right (a flag is not a socket). But it was asked once,
from `resume()`, and `kick()` is `startService` — the service still has to go foreground, read its
prefs and open a socket on its work thread, all after the plugin call has returned. So the first
answer is false nearly every time, the page opens its own sockets, and nothing ever asked again: the
page owns signing for the whole session.

  offers-again      a refused hand-over is retried, not accepted as final
  stands-down-once  the moment one is accepted, the page closes its sockets — and stops asking
  never-both        `_standDown` runs ONLY on a true receipt: two signers answering one request
                    publishes two events, which is what the whole hand-over protocol prevents
  gives-up          a phone with no service at all must not retry for ever

The method is EXTRACTED from app.js, so it cannot drift from what ships.
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


def _offer():
    with open(APP) as fh:
        src = fh.read()
    # Anchored to the signer half — `_offerNative` is unique, but the file has more than one object
    # with a `revive`, and an extraction that grabs the wrong one passes while proving nothing.
    src = src[src.index("  const Nip46Signer = {"):]
    i = src.index("    _offerNative(){")
    depth, j, started = 0, i, False
    while j < len(src):
        if src[j] == "{":
            depth += 1
            started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                break
        j += 1
    at = re.search(r"_offerAt: \[[^\]]*\],", src)
    assert at, "the offer ladder is gone"
    return at.group(0) + "\n    _offering: false,\n" + src[i:j + 1] + ","


PAGE = """<!doctype html><meta charset="utf-8"><pre id="out"></pre><script>
const sleep = ms => new Promise(r=>setTimeout(r,ms));

function makeSigner(takesAfter){
  return {
    active: true, nativeOn: false, pushes: 0, downs: 0, syncs: 0,
    async _pushNative(){
      this.pushes++;
      // The service takes the job on the Nth offer — 0 means "there is no service at all".
      if (takesAfter && this.pushes >= takesAfter){ this.nativeOn = true; return true; }
      return false;
    },
    _standDown(){ this.downs++; },
    _sync(){ this.syncs++; },
    __OFFER__
  };
}

(async () => {
  const out = {};
  // The real ladder is [2500, 6000, 15000, 40000]; shortened here so the test is not 60s long. The
  // RULE under test is "it asks again and stops on success", not the exact spacing.
  const FAST = [5, 5, 5, 5];

  // 1. THE BUG: the first offer is refused (the service has not opened its socket yet), the second
  //    is accepted. The page must hand over.
  {
    const s = makeSigner(2); s._offerAt = FAST;
    s._offerNative(); await sleep(120);
    out.retriedPushes = s.pushes;
    out.retriedDowns = s.downs;
    out.retriedNative = s.nativeOn;
  }

  // 2. Accepted on the FIRST offer: exactly one stand-down, and no further asking.
  {
    const s = makeSigner(1); s._offerAt = FAST;
    s._offerNative(); await sleep(120);
    out.firstPushes = s.pushes;
    out.firstDowns = s.downs;
  }

  // 3. No service at all (a browser, the desktop build, an APK without the plugin): it must give up
  //    rather than poll for ever, and must NEVER stand the page down.
  {
    const s = makeSigner(0); s._offerAt = FAST;
    s._offerNative(); await sleep(200);
    out.noneDowns = s.downs;
    out.nonePushes = s.pushes;
    out.noneOffering = s._offering;
  }

  // 4. Two callers (resume() and a foreground) must not start two ladders.
  {
    const s = makeSigner(0); s._offerAt = FAST;
    s._offerNative(); s._offerNative(); s._offerNative();
    await sleep(200);
    out.doublePushes = s.pushes;
  }

  document.getElementById('out').textContent = JSON.stringify(out);
})().catch(e => { document.getElementById('out').textContent = JSON.stringify({err:String(e)}); });
</script>"""


class SignerHandoverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        chrome = (shutil.which("google-chrome-stable") or shutil.which("chromium")
                  or shutil.which("google-chrome") or shutil.which("chrome"))
        if not chrome:
            raise unittest.SkipTest("no chrome")
        tmp = tempfile.mkdtemp(prefix="pchand-")
        try:
            path = os.path.join(tmp, "t.html")
            with open(path, "w") as fh:
                fh.write(PAGE.replace("__OFFER__", _offer()))
            res = subprocess.run(
                [chrome, "--headless", "--no-sandbox", "--disable-gpu",
                 "--virtual-time-budget=15000", "--dump-dom", "file://" + path],
                capture_output=True, text=True, timeout=180).stdout
            m = re.search(r'<pre id="out">(.*?)</pre>', res, re.S)
            if not m or not m.group(1).strip():
                raise unittest.SkipTest("page did not evaluate")
            cls.r = json.loads(m.group(1))
            assert "err" not in cls.r, cls.r["err"]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_a_refused_handover_is_offered_again(self):
        """THE BUG. One refusal used to mean the page signed for the rest of the session — fast on
        screen, throttled to about one request a minute behind it."""
        self.assertGreaterEqual(self.r["retriedPushes"], 2, "the offer was never repeated")
        self.assertEqual(self.r["retriedDowns"], 1, "the page did not hand over when it could")
        self.assertTrue(self.r["retriedNative"])

    def test_an_accepted_handover_stands_down_once_and_stops_asking(self):
        self.assertEqual(self.r["firstPushes"], 1)
        self.assertEqual(self.r["firstDowns"], 1)

    def test_a_phone_with_no_service_gives_up_and_keeps_signing(self):
        """A browser, the desktop build, or an APK without the plugin. Standing down there would
        leave NOTHING listening — every paired app waiting on a signer that no longer exists."""
        self.assertEqual(self.r["noneDowns"], 0, "the page stood down with nothing to hand over to")
        self.assertLessEqual(self.r["nonePushes"], 5, "it is still polling")
        self.assertFalse(self.r["noneOffering"], "the ladder never finished")

    def test_two_callers_do_not_start_two_ladders(self):
        """resume() and a foreground can both ask. Two ladders would double every offer, and each
        offer is a real bridge call that starts a service."""
        self.assertLessEqual(self.r["doublePushes"], 5)


if __name__ == "__main__":
    unittest.main()
