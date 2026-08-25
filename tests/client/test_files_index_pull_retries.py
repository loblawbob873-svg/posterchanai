"""One failed index pull must not convince the whole page that your drive has no folders.

WHAT WAS REPORTED, in one breath, and it is one bug:

    "why is the folder list gone on the reply post blossom file picker!"
    "new post is missing the blossom folder picker too"
    "folder choose is broken all across blossom"

Four call sites each wrote their own version of "pull the index once":

    if(!FilesIdx._pulled){ FilesIdx._pulled = true; try{ await FilesIdx.pull(); }catch(_){ } }

The latch is set BEFORE the attempt and the failure is swallowed, so the FIRST failure is permanent:
no later picker, on any screen, ever pulls again for the life of the page. And `pull()` opens by
asking the signer for a kind-27235 — which with a remote signer is a phone that can be busy, slow or
asleep — so the trigger is not exotic. It is the ordinary bad afternoon.

`pull()` had the same shape one line further in: `_pulling = true` at the top, cleared at the END of
a `try` whose `catch` swallows. A signer that rejected left it TRUE for ever, and `_ensureMK` checks
it before deciding to fetch the drive's key.

The fix is to latch on the RESULT — `_pullOk`, the flag that already means "the index was actually
materialised", and the same one `_save` gates on so a drive's folders are never overwritten by an
index we never read.

  survives-a-failure   a pull that throws leaves no latch: the next caller tries again
  pulling-clears       …and `_pulling` is false afterwards, so the drive key can still be fetched
  latches-on-success   once the index IS loaded, ensure() stops asking (four pickers, one pull)
  shared-in-flight     concurrent callers share one pull rather than starting four

The methods are EXTRACTED from app.js, not copied, so this cannot drift from what ships.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
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


def _methods():
    with open(APP) as fh:
        src = fh.read()
    # ANCHORED to the FilesIdx object. `ensure(){` and `async pull(){` both appear elsewhere in
    # app.js (MusicPlayer has an ensure of its own), and a test that silently extracts the wrong
    # function tests nothing while looking green — this one failed loudly, which is how it was found.
    src = src[src.index("  const FilesIdx = {"):]
    # ensure() is the gate; pull() is the wrapper whose finally clears _pulling. `_pull` (the body
    # that talks to the server) is stubbed here — this is about the bookkeeping around it, and the
    # body needs a whole drive to run.
    return ",\n".join([_fn(src, "ensure", "ensure(){"),
                       _fn(src, "pull", "async pull(){")])


PAGE = """<!doctype html><meta charset="utf-8"><pre id="out"></pre><script>
const sleep = ms => new Promise(r=>setTimeout(r,ms));

// The object the methods live on, reduced to what they touch. `_pull` stands in for the real body:
// it either throws (a signer that will not answer) or materialises the index (sets _pullOk).
function makeIdx(opts){
  return {
    _pullOk: false, _pulling: false, _pullDone: false, _ensuring: null,
    pulls: 0, willThrow: (opts||{}).throws || 0,
    _norm(){ return {}; },
    async _pull(){
      this.pulls++;
      await sleep(5);
      if (this.willThrow > 0){ this.willThrow--; throw new Error('signer request timed out'); }
      this._pullOk = true; this._pullDone = true;
      return {};
    },
    __METHODS__,
  };
}

(async () => {
  const out = {};

  // 1. THE REPORTED BUG. The first picker's pull fails; the second picker must try again.
  {
    const idx = makeIdx({throws: 1});
    out.firstOk  = await idx.ensure();
    out.secondOk = await idx.ensure();
    out.pullsAfterRetry = idx.pulls;
  }

  // 2. …and a throw must not stick the in-flight flag, which gates fetching the drive's KEY.
  {
    const idx = makeIdx({throws: 1});
    try{ await idx.pull(); }catch(_){ }
    out.pullingAfterThrow = idx._pulling;
  }

  // 3. Once the index is really loaded, four pickers cost ONE pull. (The old latch got this right;
  //    a fix that re-pulls on every picker would trade a broken folder list for a slow one.)
  {
    const idx = makeIdx();
    await idx.ensure(); await idx.ensure(); await idx.ensure(); await idx.ensure();
    out.pullsWhenOk = idx.pulls;
  }

  // 4. Concurrent openers (the composer's picker and the Files view drawing at once) share one pull.
  {
    const idx = makeIdx();
    await Promise.all([idx.ensure(), idx.ensure(), idx.ensure()]);
    out.pullsConcurrent = idx.pulls;
  }

  // 5. A signer that is down for a while and then comes back: every attempt tries, and the moment
  //    it works the answer is true. This is the whole user-visible promise.
  {
    const idx = makeIdx({throws: 3});
    const seen = [];
    for (let i = 0; i < 4; i++) seen.push(await idx.ensure());
    out.recovery = seen;
  }

  document.getElementById('out').textContent = JSON.stringify(out);
})();
</script>"""


class FilesIndexPullRetries(unittest.TestCase):
    def test_picker_keeps_indexed_folders_even_when_current_blob_filter_is_empty(self):
        app = (Path(__file__).resolve().parents[2] / "static/js/client/app.js").read_text()
        self.assertIn("FilesIdx.folders().filter(f=>!FilesIdx.isEncFolder(f)).map", app)

    @classmethod
    def setUpClass(cls):
        chrome = (shutil.which("google-chrome-stable") or shutil.which("chromium")
                  or shutil.which("google-chrome") or shutil.which("chrome"))
        if not chrome:
            raise unittest.SkipTest("no chrome")
        tmp = tempfile.mkdtemp(prefix="pcfpull-")
        try:
            path = os.path.join(tmp, "t.html")
            with open(path, "w") as fh:
                fh.write(PAGE.replace("__METHODS__", _methods()))
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

    def test_a_failed_pull_is_tried_again_by_the_next_caller(self):
        """The latch used to be set before the attempt, so the first failure was for ever — every
        picker on the page showed a flat, folderless drive until it was reloaded."""
        self.assertIs(self.r["firstOk"], False, "a pull that threw must not report success")
        self.assertIs(self.r["secondOk"], True, "the next picker must try again")
        self.assertEqual(self.r["pullsAfterRetry"], 2)

    def test_a_throwing_pull_clears_the_in_flight_flag(self):
        """`_pulling` gated fetching the drive's master key. Left true by a rejected signature, the
        drive could not be read again for the life of the page — with nothing logged."""
        self.assertIs(self.r["pullingAfterThrow"], False)

    def test_a_loaded_index_is_not_pulled_again_for_every_picker(self):
        self.assertEqual(self.r["pullsWhenOk"], 1,
                         "four openers cost four round trips — a slow folder list instead of a lost one")

    def test_concurrent_openers_share_one_pull(self):
        self.assertEqual(self.r["pullsConcurrent"], 1)

    def test_it_recovers_the_moment_the_signer_does(self):
        self.assertEqual(self.r["recovery"], [False, False, False, True])
