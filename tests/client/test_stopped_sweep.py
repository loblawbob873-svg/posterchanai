"""A paused folder must not answer "in step", and Check must not disagree with Sync now.

Reported from a laptop holding a folder it had not yet received:

    "i paused laptop, did a tidy check, then sync now, 'in step nothing to sync'"
    "check would download all this shit if you click on check but sync now says nothing to sync"

Both buttons run the same engine. What differed is where each one lands relative to the stop check:
Check returns its plan before any transfer begins, and a real sweep asks `shouldStop()` before its
first file. Pause set that flag and only the Start button ever cleared it — so from then on every
"Sync now" halted instantly, and a halted sweep, having done nothing, fell through every line of the
summary to the single most reassuring sentence the card can print.

The engine half is covered by exec_sim.js ("an interrupted sweep resumes where it stopped"). The two
client-side halves are here: `summarise` is lifted out of sync.js and RUN against real reports, and
the flag-clearing is pinned structurally, below the `running` guard — the one place it is safe, since
nothing can be sweeping there.
"""
import json
import os
import re
import subprocess  # noqa: F401
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SYNC = os.path.join(ROOT, "static", "js", "client", "sync.js")


def _fn(name):
    """Lift one top-level function out of sync.js and hand it back as a callable under node."""
    src = open(SYNC, encoding="utf-8").read()
    at = src.index("function " + name + "(")
    i, depth = src.index("{", at), 0
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return src[at:i + 1]


def summarise(rep):
    body = _fn("summarise")
    js = ("%s\nconst r = summarise(%s, {});\nprocess.stdout.write(String(r));"
          % (body, json.dumps(rep)))
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise AssertionError(out.stderr)
    return out.stdout


BLANK = {"uploaded": [], "downloaded": [], "trashed": [], "conflicted": [], "failed": [],
         "skipped": [], "removedRemote": [], "unchanged": 0}


class StoppedSweepTests(unittest.TestCase):
    def test_a_halted_sweep_is_never_reported_as_in_step(self):
        line = summarise(dict(BLANK, stopped=True))
        self.assertNotIn("in step", line, "a sweep that was stopped claimed the folder is in step")
        self.assertIn("stopped", line)

    def test_it_says_what_it_managed_before_it_stopped(self):
        line = summarise(dict(BLANK, stopped=True, downloaded=["a", "b", "c"]))
        self.assertIn("3 down", line)
        self.assertIn("stopped", line)

    def test_a_finished_idle_sweep_still_says_in_step(self):
        """The guard must not swallow the ordinary answer — that is the one people read most."""
        line = summarise(dict(BLANK, unchanged=4200))
        self.assertIn("in step", line)
        self.assertIn("4200 files checked", line)

    def test_a_finished_working_sweep_is_unchanged(self):
        line = summarise(dict(BLANK, downloaded=["a"], uploaded=["b", "c"]))
        self.assertIn("2 up", line)
        self.assertIn("1 down", line)
        self.assertNotIn("stopped", line)


class StopFlagTests(unittest.TestCase):
    def setUp(self):
        self.src = open(SYNC, encoding="utf-8").read()

    def test_a_manual_sweep_clears_the_stop_flag(self):
        self.assertIn("if(o.manual && !o.dryRun) stopping.delete(f.id);", self.src,
                      "pressing Sync now no longer clears a leftover stop, so a paused folder "
                      "halts on its first check for ever")

    def test_it_clears_it_below_the_running_guard(self):
        """Above it, this would cancel the stop of a sweep that is still going — the opposite bug."""
        guard = self.src.index("if(running.has(f.id)){")
        clear = self.src.index("if(o.manual && !o.dryRun) stopping.delete(f.id);")
        after = self.src.index("running.set(f.id, job);")
        self.assertGreater(clear, guard, "the stop is cleared before the already-running check")
        self.assertLess(clear, after, "the stop is cleared after the sweep has been registered")

    def test_pause_still_stops_the_sweep_that_is_running(self):
        self.assertIn("stopping.add(id);", self.src)


class RepairTests(unittest.TestCase):
    """Repair may only trash a damaged file when the store can actually replace it.

    "Check my files" finds a local copy whose bytes do not match what the devices agreed, and the
    repair is to set it aside so the next sweep fetches a fresh one. That is only a repair if there
    IS one: if the store has lost those bytes, this moves somebody's only copy into `.pc-trash` on
    the strength of a checksum saying it is damaged — which it may be in a way they would still
    rather have than nothing.
    """

    def setUp(self):
        self.src = open(SYNC, encoding="utf-8").read()
        at = self.src.index("async function verifyFolder(f){")
        self.body = self.src[at:self.src.index("\n  function ", at)]

    def test_it_asks_the_store_before_it_trashes(self):
        ask = self.body.index("docs.hasBlob(")
        trash = self.body.index("fs.trash(")
        self.assertLess(ask, trash, "it sets the file aside before checking there is a replacement")

    def test_a_file_the_store_cannot_replace_is_left_alone_and_named(self):
        self.assertIn("stranded.push(p); continue;", self.body)
        self.assertIn("left alone", self.body)

    def test_an_unknown_answer_is_not_treated_as_missing(self):
        """A HEAD that fails is "I could not ask", and refusing to repair on that would make a blip
        look like data loss — the same confusion, pointed the other way."""
        self.assertIn("catch(_){ ok = true; }", self.body)


class RefusalWordingTests(unittest.TestCase):
    """A mass delete and an unreadable device are refused by the same rule and mean opposite things.

    One is "your other devices really did delete these". The other is "one of your devices did not
    answer, so I cannot know". They shared a sentence, so the second case told somebody their files
    had been deleted elsewhere when nothing of the sort had happened.
    """

    def line(self, rep):
        return summarise(dict(BLANK, **rep))

    def test_an_unreadable_device_does_not_claim_the_others_deleted_anything(self):
        line = self.line({"refusedTrash": {"kind": "partialViews", "n": 40}})
        self.assertIn("could not be read", line)
        self.assertNotIn("say are deleted", line)

    def test_a_real_mass_delete_still_says_so(self):
        line = self.line({"refusedTrash": {"kind": "massTrash", "n": 40, "keep": 0}})
        self.assertIn("say are deleted", line)

    def test_deletions_held_back_from_the_others_are_reported_too(self):
        line = self.line({"refusedRemoteDelete": {"kind": "partialViewsOut", "n": 12}})
        self.assertIn("did not publish 12 deletions", line)


class VerifyRepairsTests(unittest.TestCase):
    """Two different faults, two opposite repairs, and getting them the wrong way round loses data.

    A file whose LOCAL bytes are damaged is repaired by fetching a fresh copy. A file whose bytes the
    STORE no longer has, on a device that still holds it, is repaired by sending it again — fetching
    there is not a repair, it is the failure. Measured on a real folder: entries naming blobs that
    existed on neither node, so every other device planned a download, got a 404 and reported a
    failure on every sweep, while the device holding the file saw nothing wrong at all.
    """

    def setUp(self):
        self.src = open(SYNC, encoding="utf-8").read()
        at = self.src.index("async function verifyFolder(f){")
        self.body = self.src[at:self.src.index("\n  function ", at)]

    def test_bytes_the_store_lost_are_sent_again_not_fetched(self):
        send = self.body.index("const gone = (v.missingBytes || [])")
        fetch = self.body.index("const bad = v.corrupt.map(")
        self.assertLess(send, fetch, "the re-upload repair runs after the re-download one")
        self.assertIn("swept(f, { manual: true, resend: gone });", self.body[send:fetch],
                      "the repair edits the journal instead of asking for a send — which settles "
                      "as 'same content both sides' and uploads nothing")

    def test_it_only_offers_that_for_files_this_device_still_has(self):
        """A path missing HERE and missing from the store cannot be sent by this device."""
        self.assertIn("const here = new Set(v.missingHere || []);", self.body)
        self.assertIn("filter(p => !here.has(p))", self.body)

    def test_it_asks_first_and_says_nothing_is_deleted(self):
        seg = self.body[self.body.index("const gone = (v.missingBytes || [])"):]
        self.assertIn("uiConfirm", seg)
        self.assertIn("Nothing is deleted", seg)

    def test_the_three_controls_do_not_all_say_check(self):
        """Preview / Deep check / Verify — reported as "why is there Check and Check my files?"."""
        self.assertIn(">Preview</button>", self.src)
        self.assertIn(">Verify</button>", self.src)
        self.assertNotIn(">Check</button>", self.src)
        self.assertNotIn("Check my files", self.src)
