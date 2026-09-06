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


# --- AND THEN "oom" WAS ALL WE HAD, WHICH IS NOT ENOUGH TO ACT ON. -----------------------------
#
# Naming the reason was the right first step and it stopped there: 'oom' says the renderer ran out
# of memory and nothing about what had been allocated. The dialog then filled that gap with a GUESS
# written from here -- "this usually means a very large file was being synced" -- printed to the one
# person who could check it, and repeated back to us as if it were a finding.
#
# The page now takes a reading of itself every two seconds and the handler reports the curve. That
# separates the three failures that all surface as the same word: a JS heap against its ceiling (a
# document, a cache, a leak), a small heap under a dead process (a working set full of pixels), and
# a runaway node count, which is invisible in both.


def test_the_dialog_carries_the_heap_reading_not_a_theory():
    climb = [{"up": 3, "used": 40, "heap": 60, "cap": 2048, "nodes": 900, "url": "/index.html"},
             {"up": 5, "used": 1900, "heap": 1980, "cap": 2048, "nodes": 21000, "url": "/index.html"}]
    r = _run([["oom", -536870904, climb]])
    body = r["dialogs"][0]["body"]
    assert "40MB → 1900MB" in body, body
    assert "2048MB ceiling" in body, body
    assert "21000 DOM nodes" in body, body
    assert "very large file" not in body, "the guess came back: " + body


def test_a_death_with_no_reading_says_that_rather_than_zero():
    """A renderer that dies before the first poll has no curve. Reporting 0MB would read as an
    empty heap, which is a measurement -- and the opposite of the one that was taken."""
    r = _run([["oom", None]])
    body = r["dialogs"][0]["body"]
    assert "No memory reading was taken" in body, body
    assert "0MB" not in body, body


def test_the_reading_reaches_a_repeat_too():
    """The third strike is the case worth reporting, so it must not be the one that drops the
    numbers."""
    s = [{"up": 2, "used": 1500, "heap": 1600, "cap": 1536, "nodes": 4000, "url": "/index.html"}]
    r = _run([["crashed", 1, s], ["crashed", 1, s], ["crashed", 1, s]])
    assert r["dialogs"][-1]["title"] == "PosterChan keeps stopping"
    assert "1500MB" in r["dialogs"][-1]["body"], r["dialogs"][-1]["body"]


def test_a_file_is_written_and_the_person_is_told_where():
    r = _run([["oom", 3, [{"up": 9, "used": 700, "heap": 800, "cap": 4096, "nodes": 12000,
                           "url": "/index.html"}]]])
    assert "crash-report.txt" in r["dialogs"][0]["body"], r["dialogs"][0]["body"]
    assert "+9s  heap 700/800 of 4096MB  nodes 12000" in r["report"], r["report"]
    assert "renderer stopped: oom (exit 3)" in r["report"], r["report"]


def test_the_file_keeps_every_crash_of_a_run():
    """One file, appended -- a folder of near-identical reports hides a repeat as well as no file."""
    r = _run([["crashed", 1], ["oom", 2]])
    assert "renderer stopped: crashed" in r["report"], r["report"]
    assert "renderer stopped: oom" in r["report"], r["report"]
