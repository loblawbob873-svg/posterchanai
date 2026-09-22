"""A REFUSED SAVE MUST NAME WHICH PROBLEM IT HIT — AND THE FLAG THAT KNOWS MUST NOT LATCH.

`FilesIdx._saveOnce` refuses to write when it has not CONFIRMED what is on the server, which is the
guard that stops one browser holding the empty default from replacing every folder and filename on
a replaceable doc. That guard is right and is not what this is about.

What was wrong is that it could not say WHY, and the flag that knew was dead:

  * `_pullBlocked` — "the server HAS an index and this device could not READ it" (no drive key, or
    an index blob that would not fetch or decrypt) — was SET on every pull and READ BY NOTHING.
    Grepped: assignments in `pull()`, a reset, and mentions in test docstrings. No code path
    consulted it, which is how it came to read like a missing safety guard.
  * So both refusals printed the same sentence, ending "Try reloading." For the blocked case that
    is the one thing that cannot work: the index is unreadable because of THIS DEVICE'S KEY, and
    reloading re-runs the same failure. People were sent to repeat the action that could not help.
  * And `_pullBlocked` only ever went TRUE. Nothing cleared it on a successful read, so the moment
    anything consulted it — like the message below now does — it would go on blaming an unreadable
    index for the rest of the session, long after the read recovered.

THE RULES:
  blocked-says-unlock    an unreadable server index says so, and does NOT say "try reloading"
  unreachable-says-retry everything else keeps the reload advice, which is right for it
  refusal-writes-nothing neither refusal sends a request — the whole point is not overwriting
  never-latches          in pull(), no branch may set `_pullOk = true` without also clearing
                         `_pullBlocked`; a diagnosis that cannot be withdrawn is a wrong diagnosis
                         waiting to happen

`_saveOnce` is EXTRACTED from the shipped app.js, never copied, so it cannot drift from what ships.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from tests.client_source import client_source

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


PAGE = """<!doctype html><meta charset="utf-8"><script>
const toasts = [];
const toast = m => toasts.push(String(m));
const warns = [];
console.warn = m => warns.push(String(m));
let ME = { pubkey: 'ab'.repeat(32) };
let requests = 0;
const sign = async () => ({ id: 'e' });
const selfProof = async () => sign();
const uiConfirm = async () => true;
const fetch = async () => { requests++; return { ok: true, status: 200, json: async () => ({}) }; };
const uploadBlob = async () => 'https://x/' + 'cd'.repeat(64 / 2);
const _masterEncrypt = async (mk, b) => b;
const _shaFromUrl = u => (String(u).match(/([0-9a-f]{64})/i) || [, ''])[1];

// The drive, reduced to what _saveOnce touches. The pull STAYS FAILED in both scenarios: this is
// about what the app says when it genuinely cannot confirm the server, not about recovery.
function makeIdx(blocked){
  return {
    data: { folders: ['Music', 'Backgrounds'], files: { a: { name: 'x' } }, encFolders: [] },
    _pullOk: false, _pullBlocked: blocked, _dirty: true, _saving: false, _batch: false,
    _indexShas: new Set(), _mkWrapped: 'wrapped', _forceOk: false, _saveFailed: false,
    _retryT: null, _retryN: 0, _saveAgain: false, _savingP: null, _syncedAt: 0,
    _synced(){ this._syncedAt = 1; },
    _norm(){ return this.data; },
    saveLocal(){},
    async pull(){ return this.data; },        // still cannot read it
    async _ensureMK(){ return new Uint8Array(32); },
    __SAVE__,
  };
}

(async () => {
  const out = {};
  { const idx = makeIdx(true);  requests = 0; toasts.length = 0; warns.length = 0;
    out.blockedResult = await idx._saveOnce();
    out.blockedToast = toasts.join(' | '); out.blockedWarn = warns.join(' | ');
    out.blockedRequests = requests; }
  { const idx = makeIdx(false); requests = 0; toasts.length = 0; warns.length = 0;
    out.plainResult = await idx._saveOnce();
    out.plainToast = toasts.join(' | '); out.plainWarn = warns.join(' | ');
    out.plainRequests = requests; }
  console.log(JSON.stringify(out));
})();
</script>"""


def _run():
    src = client_source()
    save_once = _fn(src, "_saveOnce", "async _saveOnce(){")
    js = PAGE.replace("__SAVE__", save_once)
    js = js[js.index("<script>") + len("<script>"):js.rindex("</script>")]
    d = tempfile.mkdtemp(prefix="pc-idx-refusal-")
    try:
        p = os.path.join(d, "t.mjs")
        with open(p, "w") as fh:
            fh.write(js)
        r = subprocess.run(["node", p], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-2000:]
        return json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(d, ignore_errors=True)


class UnreadableIndexSaysWhy(unittest.TestCase):
    def test_the_two_refusals_are_two_different_sentences(self):
        out = _run()

        self.assertIs(out["blockedResult"], False, "an unconfirmed server must refuse the save")
        self.assertIs(out["plainResult"], False, "an unconfirmed server must refuse the save")

        # refusal-writes-nothing — the entire purpose is that nothing is overwritten.
        self.assertEqual(out["blockedRequests"], 0,
                         "a refused save still sent a request: %r" % out["blockedRequests"])
        self.assertEqual(out["plainRequests"], 0,
                         "a refused save still sent a request: %r" % out["plainRequests"])

        # blocked-says-unlock
        blocked = out["blockedToast"].lower()
        self.assertTrue(blocked, "the blocked refusal said nothing at all")
        self.assertIn("unlock", blocked,
                      "an index this device cannot READ must say so; it said: %r" % out["blockedToast"])
        self.assertNotIn("reloading", blocked,
                         "reloading cannot fix a drive-key problem — telling someone to reload sends "
                         "them to repeat the one thing that cannot work: %r" % out["blockedToast"])

        # unreachable-says-retry
        plain = out["plainToast"].lower()
        self.assertIn("reloading", plain,
                      "a server we could not ask is exactly the case where reloading helps; "
                      "it said: %r" % out["plainToast"])
        self.assertNotEqual(out["blockedToast"], out["plainToast"],
                            "both refusals still print the same sentence — the flag that tells them "
                            "apart is being ignored again")

        # The log has to distinguish them too; that is where this gets diagnosed from a report.
        self.assertNotEqual(out["blockedWarn"], out["plainWarn"],
                            "console.warn says the same thing for both causes")

    def test_a_successful_pull_withdraws_the_diagnosis(self):
        """never-latches — a flag that can only go TRUE is a wrong answer waiting to happen."""
        src = client_source()
        # There are two `async pull(` in app.js — the Store's and the drive's — and pull() contains
        # braces inside strings and regexes, which a brace counter mis-bounds. Take the REGION
        # between the drive's pull() and the method that follows it; if either anchor moves this
        # fails loudly rather than quietly reading nothing.
        drive = src[src.index("_pullDone:false"):]
        start = drive.index("async pull(){")
        end = drive.index("\n    push(){", start)
        pull = drive[start:end]
        sets_ok = [ln for ln in pull.splitlines() if re.search(r"_pullOk\s*=\s*true", ln)]
        self.assertTrue(sets_ok, "pull() no longer sets _pullOk — this test is pinned to the wrong code")
        for ln in sets_ok:
            self.assertIn("_pullBlocked=false", ln.replace(" ", ""),
                          "pull() marks the read CONFIRMED without withdrawing _pullBlocked, so a "
                          "recovered drive keeps being reported as unreadable for the rest of the "
                          "session:\n    %s" % ln.strip())


if __name__ == "__main__":
    unittest.main()
