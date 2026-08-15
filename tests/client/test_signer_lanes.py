"""Background decrypts must never take the transport away from what the user is doing.

TWO REPORTS, one rule. First: a `sign_event` sat behind hundreds of queued DM decrypts and the
composer did nothing for minutes — fixed by giving interactive work its own lane and its own slots.
Then, the moment the restore got fast enough to saturate the link: **"messages is great now, now
sending posts goes into pending"**. Separate lanes stopped the composer QUEUEING behind a restore;
they did not stop it COMPETING with one. Twelve decrypts in flight is twelve more publishes on the
same socket and twelve more replies to parse on the same main thread, and `Relay.publish` gives the
relay 8 seconds to acknowledge a note before the client files it as Pending.

So the bulk lane yields: while anything interactive is outstanding, no NEW background decrypt is
started. In flight work finishes, and the restore resumes a moment later — it is catching up on
things that were already said.

  yields        a queued or in-flight interactive request stops new bulk work starting
  resumes       …and the moment it settles, the restore carries on by itself
  never-starves interactive work is never blocked by bulk work, in either direction
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


def _pump():
    with open(APP) as fh:
        src = fh.read()
    src = src[src.index("  const Nip46 = {"):]          # the CLIENT half, not the signer half
    i = src.index("    _pump(){")
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
    caps = re.search(r"_cap: \d+, _capP: \d+, _inflight: 0, _inflightP: 0, _queue: \[\], _queueP: \[\],", src)
    assert caps, "the lane fields are gone"
    return caps.group(0) + "\n" + src[i:j + 1] + ","


PAGE = """<!doctype html><meta charset="utf-8"><pre id="out"></pre><script>
const sleep = ms => new Promise(r=>setTimeout(r,ms));

function makeTransport(){
  const T = {
    started: [],
    __PUMP__
    // A job that never settles unless we settle it, so the test controls the timeline exactly.
    _job(name){
      const j = { run: () => new Promise(r => { j.done = r; }), res(){}, rej(){} };
      j.run = (function(orig){ return () => { T.started.push(name); return orig(); }; })(j.run);
      return j;
    },
  };
  return T;
}

(async () => {
  const out = {};

  // 1. A user action is outstanding: no new BULK work may start.
  {
    const T = makeTransport();
    const p = T._job('sign'); T._queueP.push(p);
    for (let i = 0; i < 5; i++) T._queue.push(T._job('bulk' + i));
    T._pump(); await sleep(20);
    out.whileUser = T.started.slice();
  }

  // 2. …and the moment it settles, the restore resumes on its own.
  {
    const T = makeTransport();
    const p = T._job('sign'); T._queueP.push(p);
    for (let i = 0; i < 3; i++) T._queue.push(T._job('bulk' + i));
    T._pump(); await sleep(20);
    const before = T.started.length;
    T._inflightP--; p.done();            // the signature came back
    T._pump(); await sleep(20);
    out.beforeSettle = before;
    out.afterSettle = T.started.length;
  }

  // 3. The reverse must never happen: bulk work in flight must not delay a user action.
  {
    const T = makeTransport();
    for (let i = 0; i < 12; i++) T._queue.push(T._job('bulk' + i));
    T._pump(); await sleep(20);
    const bulkRunning = T.started.length;
    T._queueP.push(T._job('sign'));
    T._pump(); await sleep(20);
    out.bulkRunning = bulkRunning;
    out.signStarted = T.started.indexOf('sign') >= 0;
  }

  document.getElementById('out').textContent = JSON.stringify(out);
})().catch(e => { document.getElementById('out').textContent = JSON.stringify({err:String(e)}); });
</script>"""


class SignerLaneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        chrome = (shutil.which("google-chrome-stable") or shutil.which("chromium")
                  or shutil.which("google-chrome") or shutil.which("chrome"))
        if not chrome:
            raise unittest.SkipTest("no chrome")
        tmp = tempfile.mkdtemp(prefix="pclane-")
        try:
            path = os.path.join(tmp, "t.html")
            with open(path, "w") as fh:
                fh.write(PAGE.replace("__PUMP__", _pump()))
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

    def test_the_restore_yields_while_the_user_is_waiting(self):
        """THE REPORT: a post filed as Pending because the relay had 8 seconds to answer and the
        restore was using the socket and the main thread."""
        self.assertEqual(self.r["whileUser"], ["sign"],
                         f"background decrypts started while a user action was outstanding: "
                         f"{self.r['whileUser']}")

    def test_it_resumes_by_itself_when_the_user_action_settles(self):
        """Yielding that needed a nudge to end would be a restore that silently never finishes."""
        self.assertEqual(self.r["beforeSettle"], 1)
        self.assertGreater(self.r["afterSettle"], 1, "the restore never resumed")

    def test_bulk_work_never_delays_a_user_action(self):
        """The rule only runs one way. Interactive work has its own slots precisely so a restore
        already in flight cannot hold it up — that was the first bug, and it must stay fixed."""
        self.assertEqual(self.r["bulkRunning"], 12)
        self.assertTrue(self.r["signStarted"], "a user action waited behind work in flight")


if __name__ == "__main__":
    unittest.main()
