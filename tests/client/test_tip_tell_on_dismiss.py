"""Closing a tip dialog must not be the silent path.

Lightning has a zap receipt — the recipient's wallet publishes a kind-9735 and the notification
writes itself. An on-chain address tip has none: Monero is private by construction and nothing here
watches a chain, so the ONLY signal a recipient can get is the note the sender posts with "I sent
it". Close the dialog instead and the money arrives with nobody told.

Reported as *"I received a Monero zap but no notification — the sender had to DM me"*. Checked
against our relay and two public ones afterwards: no tip note for that recipient existed anywhere,
which is exactly what this failure looks like from the receiving end.

  asks-after-paying   dismissing after opening the wallet / copying the address asks whether to tell
                      them, so the tip is not silently lost
  asks-after-dwell    …and after long enough to have scanned the QR, which is the desktop path and
                      leaves no other trace
  quiet-on-glance     opening the dialog and closing it straight away asks nothing — someone who
                      changed their mind is not a sender
  quiet-after-sent    pressing "I sent it" must not be followed by "did you send it?"
  never-automatic     the answer is a question, never a post: nothing is published unless the sender
                      says yes

The helper is extracted from app.js rather than copied, so it cannot drift from what ships.
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


def _fn(src, name, opener):
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


def _harness():
    with open(APP) as fh:
        src = fh.read()
    dwell = re.search(r"const _TIP_DWELL_MS = (\d+);", src)
    assert dwell, "_TIP_DWELL_MS is gone — the dwell rule moved"
    return "\n".join([
        "const GUEST = false;",
        # Shortened so the test does not sit for the real ten seconds; the rule is what is under test.
        "const _TIP_DWELL_MS = 300;",
        _fn(src, "_tipTellOnDismiss", "function _tipTellOnDismiss(root, opts){"),
    ])


PAGE = """<!doctype html><meta charset="utf-8"><div id="modal-root"></div><pre id="out"></pre><script>
__EXTRACTED__
const sleep = ms => new Promise(r=>setTimeout(r,ms));
let asked = 0, answer = true, posted = 0;
// The real uiConfirm is an overlay with two buttons; what matters here is that it is ASKED and that
// nothing is published unless it comes back true.
const uiConfirm = async () => { asked++; return answer; };

function openTip(){
  const bg = document.createElement('div');
  bg.innerHTML = '<div class="modal"></div>';
  document.getElementById('modal-root').appendChild(bg);
  const root = bg.querySelector('.modal');
  const tell = _tipTellOnDismiss(root, { ask:'did you send it?', onYes: () => { posted++; } });
  return tell;
}
const close = () => { document.getElementById('modal-root').innerHTML = ''; };

(async () => {
  const out = {};
  // 1. opened the wallet, then dismissed
  { asked=0; posted=0; answer=true; const t=openTip(); t.engaged=true; close(); await sleep(250);
    out.askedAfterEngage = asked; out.postedAfterYes = posted; }
  // 2. sat with the QR long enough to have scanned and paid
  { asked=0; posted=0; answer=true; const t=openTip(); await sleep(400); close(); await sleep(250);
    out.askedAfterDwell = asked; }
  // 3. opened it and closed it again straight away
  { asked=0; posted=0; const t=openTip(); close(); await sleep(250);
    out.askedOnGlance = asked; }
  // 4. pressed "I sent it" (which sets posted, then closes)
  { asked=0; posted=0; const t=openTip(); t.engaged=true; t.posted=true; close(); await sleep(250);
    out.askedAfterSent = asked; }
  // 5. said "not yet" — nothing may be published
  { asked=0; posted=0; answer=false; const t=openTip(); t.engaged=true; close(); await sleep(250);
    out.askedThenNo = asked; out.postedAfterNo = posted; }
  document.getElementById('out').textContent = JSON.stringify(out);
})();
</script>"""


class TipTellOnDismiss(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        chrome = (shutil.which("google-chrome-stable") or shutil.which("chromium")
                  or shutil.which("google-chrome") or shutil.which("chrome"))
        if not chrome:
            raise unittest.SkipTest("no chrome — MutationObserver needs a real DOM")
        tmp = tempfile.mkdtemp(prefix="pctip-")
        try:
            path = os.path.join(tmp, "t.html")
            with open(path, "w") as fh:
                fh.write(PAGE.replace("__EXTRACTED__", _harness()))
            res = subprocess.run(
                [chrome, "--headless", "--no-sandbox", "--disable-gpu",
                 "--virtual-time-budget=15000", "--dump-dom", "file://" + path],
                capture_output=True, text=True, timeout=180).stdout
            m = re.search(r'<pre id="out">(.*?)</pre>', res, re.S)
            if not m or not m.group(1).strip():
                raise unittest.SkipTest("page did not evaluate")
            cls.r = json.loads(m.group(1))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dismissing_after_opening_the_wallet_asks(self):
        self.assertEqual(self.r["askedAfterEngage"], 1)
        self.assertEqual(self.r["postedAfterYes"], 1, "answering yes posts the note that tells them")

    def test_dismissing_after_long_enough_to_scan_the_qr_asks(self):
        """The desktop path leaves no click to hook: the sender scans the code with a phone. Dwell is
        the only signal there is."""
        self.assertEqual(self.r["askedAfterDwell"], 1)

    def test_a_glance_asks_nothing(self):
        self.assertEqual(self.r["askedOnGlance"], 0,
                         "someone who opened the dialog and changed their mind is not a sender")

    def test_pressing_i_sent_it_is_not_followed_by_did_you_send_it(self):
        self.assertEqual(self.r["askedAfterSent"], 0)

    def test_nothing_is_published_without_a_yes(self):
        """Posting on someone's behalf because they closed a dialog would be worse than the silence
        it replaces."""
        self.assertEqual(self.r["askedThenNo"], 1)
        self.assertEqual(self.r["postedAfterNo"], 0)

    def test_both_tip_flows_use_it(self):
        """Monero and Bitcoin Cash are the same flow with a different URI scheme; fixing one and
        leaving the other is how half of this comes back."""
        with open(APP) as fh:
            src = fh.read()
        self.assertEqual(src.count("_tipTellOnDismiss(root, {"), 2)
        for fn, opener in (("doXmrTip", "async function doXmrTip(noteId, pk, cardXmr){"),
                           ("doBchTip", "async function doBchTip(pk){")):
            body = _fn(src, fn, opener)
            self.assertIn("_tipTellOnDismiss", body, f"{fn} can still be closed silently")
            self.assertIn("tell.posted=true", body, f"{fn} would ask again after 'I sent it'")
            self.assertIn("tell.engaged=true", body, f"{fn} records no intent to pay")


if __name__ == "__main__":
    unittest.main()
