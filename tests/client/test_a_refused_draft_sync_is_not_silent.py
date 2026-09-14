"""A SYNC THAT WAS REFUSED IS NOT A SYNC, AND THE USER HAS TO BE TOLD.

This is the shape behind "lots of my fedi replies get stuck in drafts, and do not sent". The replies
were posted — there is not one kind-1 or kind-1111 refusal in 24 hours of relay log. What failed was
the DRAFTS DOCUMENT, and it failed in silence:

    await fetch('/client/drafts', {...});   // inside catch(_){}

`fetch` does not throw on an HTTP error, so a 503 ("relay rejected the write, not saved") was
indistinguishable from success. And because `pull()` unions by id, newest-ts-wins, and deliberately
never drops a draft, a removal that never reached the server comes back from the server on the next
load. The draft resurrects, and nothing anywhere said why.

THE RULES, each verified to fail without the fix:
  retries        a refusal is retried — these are transient by nature (a superseded AUTH challenge,
                 a relay that blinked), and one blink must not cost the document
  says-so        when it still will not land, the user is TOLD once, not left to discover it by
                 watching a draft come back
  quiet-when-ok  a successful sync says nothing and does not retry
  once-only      a failing run reports once, not once per attempt

The shipped `_sync` is EXTRACTED from app.js and run under node, so it cannot drift from what ships.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")


def _sync_method():
    src = open(APP, encoding="utf-8").read()
    start = src.index("    _sync(a){")
    end = src.index("    async pull(){", start)
    body = src[start:end].rstrip().rstrip(",")
    assert "fetch('/client/drafts'" in body, "the drafts sync no longer posts to /client/drafts"
    return body


PAGE = """
const toasts = [];
const toast = m => toasts.push(String(m));
let ME = { pubkey: 'ab'.repeat(32) };
const selfProof = async () => 'proof';
let attempts = 0;
globalThis.fetch = async () => { attempts++; return RESPONSE(); };
// The real one waits 900ms then backs off 1.2s and 2.4s. Keep the SHAPE, drop the wall clock.
const _realTimeout = setTimeout;
globalThis.setTimeout = (fn, _ms) => _realTimeout(fn, 0);

const Drafts = { _t: null, _syncFailed: false, __SYNC__ };

(async () => {
  Drafts._sync([{ id: 'd1', text: 'a reply' }]);
  await new Promise(r => _realTimeout(r, 400));
  console.log(JSON.stringify({ attempts, toasts }));
})();
"""


def _run(response_js):
    js = PAGE.replace("__SYNC__", _sync_method()).replace("RESPONSE()", response_js)
    d = tempfile.mkdtemp(prefix="pc-draftsync-")
    try:
        p = os.path.join(d, "t.mjs")
        open(p, "w").write(js)
        r = subprocess.run(["node", p], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-1500:]
        return json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(d, ignore_errors=True)


OK = "({ok:true, json: async()=>({ok:true})})"
REFUSED = "({ok:false, status:503, json: async()=>({ok:false, error:'relay rejected the write, not saved'})})"
# The nastiest one: HTTP 200 carrying a refusal. fetch does not throw, res.ok is true.
SOFT_REFUSED = "({ok:true, status:200, json: async()=>({ok:false, error:'not saved'})})"


class ARefusedDraftSyncIsNotSilent(unittest.TestCase):
    def test_a_successful_sync_is_quiet_and_does_not_retry(self):
        got = _run(OK)
        self.assertEqual(got["attempts"], 1, "a successful sync retried: %r" % got)
        self.assertEqual(got["toasts"], [], "a successful sync said something: %r" % got)

    def test_a_refusal_is_retried_and_then_reported(self):
        got = _run(REFUSED)
        self.assertGreater(got["attempts"], 1,
                           "a refused drafts sync was not retried — one blink loses the document "
                           "and a sent draft comes back: %r" % got)
        self.assertTrue(got["toasts"],
                        "the drafts document could not be saved and NOTHING was said. That is how "
                        "'my replies get stuck in drafts' went undiagnosed for days: %r" % got)
        self.assertEqual(len(got["toasts"]), 1,
                         "reported once per attempt instead of once per failing run: %r" % got)

    def test_a_200_carrying_a_refusal_is_still_a_refusal(self):
        """The exact trap: fetch does not throw, and res.ok is true."""
        got = _run(SOFT_REFUSED)
        self.assertGreater(got["attempts"], 1,
                           "a 200 response whose body says ok:false was treated as success: %r" % got)
        self.assertTrue(got["toasts"], "a refusal dressed as a 200 was accepted silently: %r" % got)


if __name__ == "__main__":
    unittest.main()
