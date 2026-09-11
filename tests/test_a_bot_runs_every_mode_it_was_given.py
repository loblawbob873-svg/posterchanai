"""EVERY MODE ON THE COMMAND LINE HAS TO ACTUALLY START.

"had bot join room check logs" → the process was alive, its modes were right
(`main.py --nostr --concord`), and there was no "Starting Concord listener..." anywhere.

Every listener block in main.py reads

    if threads or has_daemon:  <run in a thread>
    else:                      <run inline>; return

which is a decision about what came BEFORE it. So the FIRST listener dispatched finds nothing
running, takes the inline branch, loops forever and returns never — and every later mode on the
same command line is unreachable, forty lines past a `while True`. The wait loop for the threaded
case already existed at the end of the section; it simply could not be reached.

Nothing was wrong with any individual block. The COMBINATION had never been exercised, because
every bot here had run exactly one listener until a Concord bot needed two.

These RUN main.py. A source-reading test would have had to know to look at the branch taken by the
first block, which is precisely the thing nobody knew to look at.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOTS = ROOT / "botframework"
PY = ROOT / "venv-unified/bin/python"
MAIN = (BOTS / "main.py").read_text(encoding="utf-8")


def _run(modes, env_extra=None, seconds=18):
    """Start a real bot process and return what it printed."""
    env = {**os.environ, "PYTHONPATH": ".",
           # A valid throwaway identity; nothing is published — both listeners are told to idle.
           "NOSTR_NSEC": os.urandom(32).hex(),
           "NOSTR_PRESENCE_ONLY": "1",
           "NOSTR_RELAYS": "ws://127.0.0.1:65530",   # nothing listens here, deliberately
           "CONCORD_INVITE": "",
           "NO_COLOR": "1", "FORCE_COLOR": "0"}
    env.update(env_extra or {})
    try:
        done = subprocess.run([str(PY), "-u", str(BOTS / "main.py"), *modes],
                              cwd=str(BOTS), env=env, capture_output=True, text=True,
                              timeout=seconds)
        return done.stdout + done.stderr
    except subprocess.TimeoutExpired as e:
        # Expected: listeners do not exit. What they SAID before the timeout is the measurement.
        return ((e.stdout or b"").decode(errors="replace")
                + (e.stderr or b"").decode(errors="replace"))


@pytest.mark.skipif(not PY.exists(), reason="needs the project venv")
def test_two_listeners_both_start():
    """THE REPORT. `--nostr --concord` ran the Nostr half and joined no rooms."""
    out = _run(["--nostr", "--concord"])
    assert "Concord listener" in out or "[concord]" in out, (
        "the Concord listener never started alongside --nostr, so a bot with a room saved does "
        "nothing and logs nothing:\n" + out[-1500:])
    assert "presence-only" in out or "Nostr listener" in out, (
        "the Nostr half stopped starting:\n" + out[-1500:])
    assert "listener(s) in parallel" in out, (
        "the process did not report running several listeners, so one of them took the main "
        "thread:\n" + out[-1500:])


@pytest.mark.skipif(not PY.exists(), reason="needs the project venv")
def test_one_listener_still_runs_inline():
    """The single-listener case is the common one and must not have grown a thread it does not
    need — the inline branch is what keeps a one-mode bot simple to reason about."""
    out = _run(["--concord"])
    assert "[concord]" in out, out[-800:]
    assert "listener(s) in parallel" not in out, (
        "a single listener is now threaded; nothing needs that and it hides crashes behind a "
        "daemon thread:\n" + out[-800:])


def test_the_branch_is_decided_by_what_was_asked_for():
    """The rule, so a new listener block copied from an old one cannot reintroduce it: the
    thread-or-inline decision must consider the modes REQUESTED, not only the ones already started.
    """
    assert "multi = len(_wanted) > 1" in MAIN, (
        "main.py no longer counts the listeners it was asked for")
    stale = len(re.findall(r"if threads or has_daemon:", MAIN))
    assert stale == 0, (
        "%d listener block(s) still decide on what came BEFORE them, so whichever is dispatched "
        "first will take the main thread and the rest will never run" % stale)
    assert "_LISTENERS = (" in MAIN
    listeners = MAIN[MAIN.index("_LISTENERS = ("):]
    listeners = listeners[:listeners.index(")")]
    for mode in ("nostr", "concord", "pleroma", "dvm", "chess"):
        assert f'"{mode}"' in listeners, (
            f"--{mode} is a listener and is missing from the count, so a bot asking for it plus "
            "one other will silently run only one of them")


def test_every_listener_flag_is_counted():
    """The reverse: a flag that starts a long-running listener and is absent from `_LISTENERS`
    reintroduces the bug for exactly that combination."""
    listeners = MAIN[MAIN.index("_LISTENERS = ("):]
    listeners = listeners[:listeners.index(")")]
    counted = set(re.findall(r'"([a-z0-9_]+)"', listeners))
    # A block that threads is a long-running listener by definition.
    blocks = set(re.findall(r"if args\.([a-z0-9_]+):\n(?:.*\n)*?.*?if threads or has_daemon or multi:",
                            MAIN))
    missing = sorted(b for b in blocks if b not in counted)
    assert not missing, (
        "these listener blocks can be threaded but are not counted as listeners, so pairing one "
        "with another mode silently runs only one: %s" % ", ".join(missing))
