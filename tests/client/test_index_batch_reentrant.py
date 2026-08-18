"""Two things batching the drive index at once must not end each other's batch.

A Joplin import batches while it uploads attachments. A folder sync batches while it uploads files.
The Music bulk delete batches while it forgets tracks. Any two of those can be running together —
reported from exactly that combination: "i got it importing my .jex file into notes, folder sync was
going on at the same time".

With a boolean flag the FIRST `endBatch` turns batching off for everybody:

    import:  beginBatch()            _batch = true
    sync:    beginBatch()            _batch = true
    sync:    endBatch()              _batch = false   ← the import is still going
    import:  ...500 more pushes...   each one now schedules a full save of the index
    import:  endBatch()              ends a batch it no longer owned

That is not a corruption, it is a stampede: the index is a single encrypted document, so every one
of those pushes is a whole re-encrypt and re-upload. On an import of a few hundred attachments while
a sweep is running it is the difference between twenty saves and a thousand.

The shipped object is RUN here, with `_save` stubbed, rather than asserted about in prose.
"""
import json
import os
import re
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "..", "static", "js", "client", "app.js")
NODE = shutil.which("node") or shutil.which("nodejs")


def _lift():
    """beginBatch/endBatch/push out of app.js's IIFE, onto a stub that counts saves."""
    with open(APP, encoding="utf-8") as fh:
        src = fh.read()
    out = {}
    for name, pat in (
        # Multi-line now: it arms a guard so a batch nobody closes cannot be held for ever.
        ("begin", r"beginBatch\(\)\{[\s\S]*?\n    \},"),
        ("end", r"async endBatch\(\)\{[\s\S]*?\n    \},"),
    ):
        m = re.search(pat, src)
        assert m, "%s moved in app.js — re-point this test" % name
        out[name] = m.group(0).rstrip().rstrip(",")
    return out


@unittest.skipIf(not NODE, "no node on this node")
class TestReentrantBatch(unittest.TestCase):
    def _run(self, script):
        parts = _lift()
        js = """
        const saves = [];
        const IDX = {
          _batch: false,
          _dirty: true,
          async _save(){ saves.push(this._batch); return true; },
          // The real push(): a save per call unless a batch is open. This is the behaviour the
          // batch exists to suppress, so it is what the test has to observe.
          push(){ if(this._batch) return; this._save(); },
          %s,
          %s,
        };
        (async () => {
          %s
          process.stdout.write(JSON.stringify({ saves: saves.length, batch: IDX._batch,
                                                n: IDX._batchN|0 }));
        })();
        """ % (parts["begin"], parts["end"], script)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout)

    def test_one_holder_behaves_exactly_as_before(self):
        out = self._run("""
          IDX.beginBatch();
          for(let i=0;i<50;i++) IDX.push();
          await IDX.endBatch();
          IDX.push();
        """)
        self.assertEqual(out["saves"], 2, "a single batch stopped collapsing its pushes")
        self.assertFalse(out["batch"])

    def test_an_inner_batch_does_not_end_the_outer_one(self):
        """The reported shape: a sync's batch finishing mid-import."""
        out = self._run("""
          IDX.beginBatch();                 // the import
          IDX.beginBatch();                 // a sweep, arriving
          for(let i=0;i<10;i++) IDX.push();
          await IDX.endBatch();             // the sweep finishes
          for(let i=0;i<500;i++) IDX.push();// the import is still going
          await IDX.endBatch();
        """)
        self.assertEqual(out["saves"], 2,
                         "the import's 500 pushes each saved the whole index (%d saves)" % out["saves"])
        self.assertFalse(out["batch"], "batching never ended")
        self.assertEqual(out["n"], 0)

    def test_the_batch_ends_when_the_last_holder_lets_go(self):
        out = self._run("""
          IDX.beginBatch(); IDX.beginBatch(); IDX.beginBatch();
          await IDX.endBatch(); await IDX.endBatch();
          IDX.push();                        // still batched — one holder left
          await IDX.endBatch();
          IDX.push();                        // now it saves
        """)
        self.assertFalse(out["batch"])
        self.assertEqual(out["saves"], 4, "saves: %d" % out["saves"])

    def test_an_unbalanced_end_cannot_drive_the_count_negative(self):
        """A caller whose beginBatch threw still calls endBatch in a finally. That must not leave the
        count at -1, where the NEXT real batch would never close."""
        out = self._run("""
          await IDX.endBatch();
          await IDX.endBatch();
          IDX.beginBatch();
          IDX.push();
          await IDX.endBatch();
          IDX.push();
        """)
        self.assertEqual(out["n"], 0)
        self.assertFalse(out["batch"])
        self.assertEqual(out["saves"], 4, "saves: %d" % out["saves"])


@unittest.skipIf(not NODE, "no node on this node")
class TestALeakedBatchCannotBePermanent(unittest.TestCase):
    """Counting made a leaked `beginBatch` worse than the boolean it replaced.

    With a flag, any later `endBatch` cleared it. Counted, a `beginBatch` whose `endBatch` is skipped
    — an early return, a throw in a caller with no `finally` — pins the count above zero for the rest
    of the session, and `push()` then returns without scheduling anything. Every later edit to the
    drive index is local-only, with no error and no retry armed.
    """

    def test_the_guard_releases_a_batch_nobody_closed(self):
        src = open(APP, encoding="utf-8").read()
        at = src.index("beginBatch(){")
        body = src[at:src.index("async endBatch()", at)]
        self.assertIn("_batchGuard", body, "a leaked batch is held for the rest of the session")
        self.assertIn("this._batchN = 0", body, "the guard does not actually release the count")
        self.assertIn("if(this._dirty) this._save();", body,
                      "it releases the batch without saving what was pending")

    def test_closing_the_last_batch_cancels_the_guard(self):
        src = open(APP, encoding="utf-8").read()
        at = src.index("async endBatch()")
        body = src[at:src.index("folders()", at)]
        self.assertIn("clearTimeout(this._batchGuard)", body,
                      "a timer is left armed after every batch, for the life of the session")
