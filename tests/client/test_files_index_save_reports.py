"""A file-list save that did not reach the server must not be reported as done.

`FilesIdx._save()` swallowed every failure into a `console.warn` and returned nothing, so callers
could not tell a completed write from one that never happened. Music's "Remove N missing" then
toasted `removed 2422 missing tracks` regardless — and it was wrong twice, on two different days,
while the server kept all 3990 entries (its own log: `REFUSED a collapsing write (3990 -> 1568)`,
then a save that threw at the signer). The user removed the same 2422 tracks twice and watched them
come back both times.

  failure-is-false   a save that throws (a remote signer that will not answer, a dead network, a
                     refused write) resolves FALSE and says so on screen — once, not on every
                     retry of a debounced save
  success-is-true    the ordinary path still resolves TRUE, and clears the failure notice
  caller-honest      the Music tidy reports the verdict it was given rather than assuming one

The method is extracted from app.js rather than copied, so it cannot drift from what ships.
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


def _save_method():
    with open(APP) as fh:
        src = fh.read()
    # The retry is part of the contract: a failed save that nobody ever tries again is the same
    # silence, one level along — and the edit that failed has usually removed its own way back
    # (once "Remove N missing" empties the local list, that button is gone).
    single = _fn(src, "_save", "_save(){")          # the single-flight wrapper
    retry = _fn(src, "_retryLater", "_retryLater(){")
    steps = re.search(r"_RETRY_STEPS: \[[^\]]*\],", src)
    assert steps, "the retry backoff is gone"
    return ",\n".join([_fn(src, "_saveOnce", "async _saveOnce(){"), single, retry,
                       steps.group(0).rstrip(",")])


PAGE = """<!doctype html><meta charset="utf-8"><pre id="out"></pre><script>
const sleep = ms => new Promise(r=>setTimeout(r,ms));
const toasts = [];
const toast = m => toasts.push(String(m));
let ME = { pubkey: 'ab'.repeat(32) };
let signOk = true, fetchStatus = 200, signs = 0, bodies = [], confirmAnswer = true, asked = 0;
const sign = async () => { signs++; if (!signOk) throw new Error('signer request timed out'); return {id:'e'}; };
// Counts, because "how many times were you asked" is the whole question in the storm case.
const uiConfirm = async () => { asked++; return confirmAnswer; };
const fetch = async (u, o) => {
  try{ bodies.push(JSON.parse((o||{}).body||'{}')); }catch(_){ }
  const last = bodies[bodies.length-1] || {};
  // The real server refuses a collapsing write UNLESS the request carries force.
  if (fetchStatus === 409)
    return last.force ? { ok:true, status:200, json: async () => ({}) }
                      : { ok:false, status:409, json: async () => ({error:'refused: 3990 -> 1568'}) };
  return { ok: fetchStatus < 400, status: fetchStatus, json: async () => ({}) };
};
const uploadBlob = async () => 'https://x/' + 'cd'.repeat(32);
const _masterEncrypt = async (mk, b) => b;
const _shaFromUrl = u => (String(u).match(/([0-9a-f]{64})/i) || [,''])[1];

// The object the method lives on, reduced to what it touches.
function makeIdx(){
  return {
    data: { folders: ['Music'], files: { a: {name:'x'} }, encFolders: [] },
    _pullOk: true, _dirty: true, _saving: false, _batch: false,
    _indexShas: new Set(), _mkWrapped: 'wrapped',
    _forceOk: false, _saveFailed: false, _retryT: null, _retryN: 0,
    _saveAgain: false, _savingP: null, _syncedAt: 0,
    _synced(){ this._syncedAt = 1; },
    _norm(){ return this.data; },
    saveLocal(){},
    async pull(){ return this.data; },
    async _ensureMK(){ return new Uint8Array(32); },
    __SAVE__,
  };
}

(async () => {
  const out = {};
  // 0. THE DIALOG STORM. Runs FIRST: a later scenario's retry timer would otherwise fire against
  //    the same stub mid-count and answer questions nobody asked. Ten saves fired at once — an upload adding a file a second while the
  //    server disagrees — must ask ONE question, not ten. A save that can open a dialog and is not
  //    single-flight turns a folder upload into hundreds of prompts.
  {
    const idx = makeIdx(); signOk = true; fetchStatus = 409; confirmAnswer = true;
    asked = 0; signs = 0; bodies.length = 0;
    await Promise.all(Array.from({length: 10}, () => idx._save()));
    out.stormAsked = asked;
  }
  // 1. the signer will not answer — the exact shape of the reported failure
  {
    const idx = makeIdx(); signOk = false; fetchStatus = 200; toasts.length = 0;
    out.failReturned = await idx._save();
    out.failToasted  = toasts.length;
    out.failDirty    = idx._dirty;
    // …and a second attempt must not toast again (a debounced save retries on a timer)
    await idx._save();
    out.failToastedTwice = toasts.length;
  }
  // 2. the server refuses the write outright
  {
    const idx = makeIdx(); signOk = true; fetchStatus = 500; toasts.length = 0;
    out.httpFailReturned = await idx._save();
  }
  // 3. the ordinary path
  {
    const idx = makeIdx(); signOk = true; fetchStatus = 200; toasts.length = 0;
    out.okReturned = await idx._save();
    out.okToasted  = toasts.length;
    out.okDirty    = idx._dirty;
  }
  // 4. a failure must schedule a RETRY — nothing else ever calls _save again, and the button that
  //    started it is gone once the local list is already clean
  {
    const idx = makeIdx(); signOk = false; fetchStatus = 200;
    idx._RETRY_STEPS = [40];                       // the rule is that it retries, not how long it waits
    await idx._save();
    out.retryArmed = !!idx._retryT;
    signOk = true;                                 // the signer comes back
    await sleep(200);
    out.retriedClean = idx._dirty === false;       // …and the pending edit went out on its own
  }
  // 5. a collapsing write, confirmed once, must not ask (or sign) twice on the way back
  {
    const idx = makeIdx(); signOk = true; fetchStatus = 409; confirmAnswer = true;
    signs = 0; bodies.length = 0;
    out.collapseSaved = await idx._save();
    out.collapseSigns = signs;                     // 409 + confirm + forced write = two
    out.collapseForced = bodies.some(b => b.force === true);
    // the same edit again (as a retry would) — one signature, no prompt
    idx._dirty = true; idx._forceOk = true; signs = 0; bodies.length = 0;
    await idx._save();
    out.retrySigns = signs;
    out.retryForcedFirst = !!(bodies[0] && bodies[0].force);
  }
  // 7. answering NO must not force anything, then or later
  {
    const idx = makeIdx(); signOk = true; fetchStatus = 409; confirmAnswer = false;
    bodies.length = 0;
    out.refusedSaved = await idx._save();
    out.refusedForceRemembered = idx._forceOk;
  }
  document.getElementById('out').textContent = JSON.stringify(out);
})();
</script>"""


class FilesIndexSaveReports(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        chrome = (shutil.which("google-chrome-stable") or shutil.which("chromium")
                  or shutil.which("google-chrome") or shutil.which("chrome"))
        if not chrome:
            raise unittest.SkipTest("no chrome")
        tmp = tempfile.mkdtemp(prefix="pcfidx-")
        try:
            path = os.path.join(tmp, "t.html")
            with open(path, "w") as fh:
                fh.write(PAGE.replace("__SAVE__", _save_method()))
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

    def test_a_save_the_signer_could_not_sign_is_reported_as_a_failure(self):
        self.assertIs(self.r["failReturned"], False,
                      "a save that threw must not answer anything a caller can read as success")
        self.assertEqual(self.r["failToasted"], 1, "the user must be told, on screen, once")
        self.assertTrue(self.r["failDirty"], "the edit stays pending so a later save retries it")

    def test_it_does_not_toast_again_on_every_retry(self):
        """Saves are debounced and retried; an offline device would otherwise toast on a timer."""
        self.assertEqual(self.r["failToastedTwice"], 1)

    def test_a_refused_write_is_reported_as_a_failure(self):
        self.assertIs(self.r["httpFailReturned"], False)

    def test_the_ordinary_save_still_reports_success_and_says_nothing(self):
        self.assertIs(self.r["okReturned"], True)
        self.assertEqual(self.r["okToasted"], 0, "a save that worked is not worth a toast")
        self.assertFalse(self.r["okDirty"])

    def test_a_failed_save_is_retried_on_its_own(self):
        """Nothing else ever calls _save again — and the edit that failed has usually removed its own
        way back, since "Remove N missing" is gone once the local list is clean. Reported as "I
        already did all that", with the server still holding every entry."""
        self.assertTrue(self.r["retryArmed"], "a failure must arm a retry")
        self.assertTrue(self.r["retriedClean"], "the pending edit must go out when the signer returns")

    def test_a_confirmed_collapse_is_not_asked_or_signed_twice(self):
        """The 409 path costs TWO signatures for one action, and with a remote signer each is a prompt
        on a phone — the second was where a confirmed mass-delete kept dying."""
        self.assertIs(self.r["collapseSaved"], True)
        self.assertEqual(self.r["collapseSigns"], 2, "first attempt + the forced one")
        self.assertTrue(self.r["collapseForced"])
        self.assertEqual(self.r["retrySigns"], 1, "a retry of a confirmed collapse is one signature")
        self.assertTrue(self.r["retryForcedFirst"], "…and carries force on the FIRST request")

    def test_ten_concurrent_saves_ask_one_question(self):
        """A save can open a "this removes most of your file list" dialog, and nothing made saves
        single-flight — so an upload adding a file a second against a disagreeing server produced a
        refused save every two seconds, each one a fresh prompt, on a screen the user was just
        adding songs to."""
        self.assertEqual(self.r["stormAsked"], 1,
                         f"{self.r['stormAsked']} dialogs for one pending edit")

    def test_answering_no_never_forces(self):
        self.assertIs(self.r["refusedSaved"], False)
        self.assertFalse(self.r["refusedForceRemembered"],
                         "a declined collapse must not be remembered as approved")

    def test_the_music_tidy_reports_the_verdict_it_was_given(self):
        """The caller is half the bug: with a truthful _save it can still claim success by ignoring
        the answer, which is exactly what it used to do."""
        with open(APP) as fh:
            src = fh.read()
        i = src.index("const dead=musicEntries(list).filter(t=>t.missing)")
        block = src[i:i + 1200]
        self.assertIn("await FilesIdx.endBatch()", block)
        self.assertRegex(block, r"const saved\s*=\s*await FilesIdx\.endBatch\(\)",
                         "the tidy must keep the verdict")
        self.assertRegex(block, r"toast\(saved\s*\?", "the toast must depend on it")


if __name__ == "__main__":
    unittest.main()
