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
    return _fn(src, "_save", "async _save(){")


PAGE = """<!doctype html><meta charset="utf-8"><pre id="out"></pre><script>
const toasts = [];
const toast = m => toasts.push(String(m));
let ME = { pubkey: 'ab'.repeat(32) };
let signOk = true, fetchStatus = 200;
const sign = async () => { if (!signOk) throw new Error('signer request timed out'); return {id:'e'}; };
const fetch = async () => ({ ok: fetchStatus < 400, status: fetchStatus, json: async () => ({}) });
const uploadBlob = async () => 'https://x/' + 'cd'.repeat(32);
const _masterEncrypt = async (mk, b) => b;
const _shaFromUrl = u => (String(u).match(/([0-9a-f]{64})/i) || [,''])[1];
const uiConfirm = async () => true;

// The object the method lives on, reduced to what it touches.
function makeIdx(){
  return {
    data: { folders: ['Music'], files: { a: {name:'x'} }, encFolders: [] },
    _pullOk: true, _dirty: true, _saving: false, _batch: false,
    _indexShas: new Set(), _mkWrapped: 'wrapped',
    _norm(){ return this.data; },
    saveLocal(){},
    async pull(){ return this.data; },
    async _ensureMK(){ return new Uint8Array(32); },
    __SAVE__,
  };
}

(async () => {
  const out = {};
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
