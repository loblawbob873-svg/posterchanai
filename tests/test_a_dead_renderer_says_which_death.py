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


def _run(reasons, answers=None):
    out = subprocess.run(["node", str(ROOT / "tests/renderer_gone_sim.mjs"), json.dumps(reasons),
                          json.dumps(answers or [])],
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
    assert r["boxes"][-1]["message"] == "PosterChan keeps stopping", r["boxes"][-1]
    assert "not be\nreloaded" in r["boxes"][-1]["detail"].replace(" ", "\n") or \
           "not be reloaded" in r["boxes"][-1]["detail"], r["boxes"][-1]


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
    assert r["boxes"][-1]["message"] == "PosterChan keeps stopping"
    assert "1500MB" in r["boxes"][-1]["detail"], r["boxes"][-1]["detail"]


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


# --- AND "QUIT AND REOPEN" WAS NOT A WAY OUT OF A BOOT LOOP. -----------------------------------
#
# Stopping the reload was the right half and it left the app unusable: a renderer that dies during
# boot dies again on the next launch, so the advice amounted to "keep doing the thing that fails".
# The third strike now offers the one lever that is generic -- this device's local cache, the only
# part of a boot whose size depends on how long the app has been used.

LOOP = [["crashed", 1], ["crashed", 1], ["crashed", 1]]


def test_the_third_strike_offers_a_way_out_and_not_only_an_apology():
    r = _run(LOOP)
    assert r["boxes"], "the third strike still just prints and gives up"
    assert any("Clear" in b for b in r["boxes"][-1]["buttons"]), r["boxes"][-1]["buttons"]


def test_nothing_is_cleared_unless_it_is_asked_for_twice():
    """Default answers are the cancel buttons. A cache wipe must never be what happens when somebody
    dismisses a dialog they did not read."""
    assert _run(LOOP)["cleared"] == []
    assert _run(LOOP, answers=[2, 0])["cleared"] == [], "the confirm was not honoured"


def test_clearing_the_cache_never_touches_the_key():
    """`localStorage` holds the session, and for an nsec login that IS the user's secret key.
    Clearing it to fix a crash would destroy the account of anybody who had not written it down."""
    r = _run(LOOP, answers=[2, 1])
    assert r["cleared"], "the confirmed clear did nothing"
    for storages in r["cleared"]:
        assert "localstorage" not in [s.lower() for s in storages], storages
    assert set(r["cleared"][0]) <= {"indexdb", "cachestorage", "serviceworkers"}, r["cleared"][0]


def test_the_second_dialog_says_what_it_costs():
    r = _run(LOOP, answers=[2, 0])
    detail = r["boxes"][-1]["detail"]
    assert "ON THIS DEVICE" in detail, detail
    assert "signed in" in detail, detail


def test_one_more_reload_is_offered_and_resets_the_count():
    """Somebody who thinks it was a one-off gets to say so, and must not be told 'three in a minute'
    again on the very next death."""
    r = _run(LOOP + [["crashed", 1]], answers=[1])
    # Two automatic reloads, the one the person asked for, and then the next death treated as the
    # one-off it now is -- rather than "three in a minute" a second later about the same minute.
    assert r["reloads"] == 4, r["reloads"]
    assert len(r["boxes"]) == 1, "it went straight back to giving up"
