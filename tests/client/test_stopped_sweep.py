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
