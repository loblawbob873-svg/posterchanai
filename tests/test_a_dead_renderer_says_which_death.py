"""THE DIALOG NAMED THE WRONG CAUSE, AND THEN LOOPED ON IT.

Reported as: "windows app crashes at start: posterchan ran out of memory".

`render-process-gone` fires with a `reason` of 'oom', 'crashed', 'killed', 'launch-failed' or
'integrity-failure'. The handler titled EVERY one of them "PosterChan ran out of memory" and only
distinguished them in the body. The title is what a person reads and repeats, so a renderer that
failed to launch -- antivirus, a GPU or sandbox refusal, the common Windows ones -- reached us, in
good faith, as a memory problem, and sent the investigation somewhere it could not go.

The second half is why it read as "crashes at start": every death was answered with a reload. A
death during BOOT therefore boots, dies, reloads, dies -- a dialog every few seconds and an app that
never becomes usable. Reloading is right for the case this was written for (a renderer killed after
hours of syncing a very large file) and wrong the moment it repeats.

The handler is EXTRACTED AND RUN here against a stubbed Electron, because the questions are what the
title says and how many times it reloads -- neither of which a source-text assertion can answer.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(reasons):
    out = subprocess.run(["node", str(ROOT / "tests/renderer_gone_sim.mjs"), json.dumps(reasons)],
                         cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_launch_failure_is_not_called_a_memory_problem():
    r = _run([["launch-failed", -1]])
    assert r["dialogs"][0]["title"] != "PosterChan ran out of memory", r["dialogs"][0]
    assert "launch-failed" in r["dialogs"][0]["body"], r["dialogs"][0]


def test_a_real_oom_still_says_so():
    r = _run([["oom", None]])
    assert r["dialogs"][0]["title"] == "PosterChan ran out of memory"


def test_the_reason_and_exit_code_reach_the_person():
    r = _run([["crashed", 134]])
    assert "crashed" in r["dialogs"][0]["body"] and "134" in r["dialogs"][0]["body"], r["dialogs"][0]


def test_a_clean_exit_says_nothing():
    r = _run([["clean-exit", 0]])
    assert r["dialogs"] == [] and r["reloads"] == 0


def test_it_reloads_a_one_off():
    r = _run([["oom", None]])
    assert r["reloads"] == 1


def test_it_stops_reloading_a_death_that_repeats():
    """Three in a minute is a boot loop, not a big file. Reloading again just shows the dialog again."""
    r = _run([["crashed", 1], ["crashed", 1], ["crashed", 1], ["crashed", 1]])
    assert r["reloads"] == 2, f"kept reloading into the same crash: {r['reloads']}"
    assert r["dialogs"][-1]["title"] == "PosterChan keeps stopping", r["dialogs"][-1]
    assert "not be reloaded" in r["dialogs"][-1]["body"]
