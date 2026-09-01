"""The shell probe must be safe to leave in the tree and honest when it cannot run.

`scripts/probe_shell.py` talks to a Chrome DevTools endpoint on a PosterChanOS desktop. Two
properties matter more than anything it prints:

  * it must not carry a default endpoint. That port is an unauthenticated debugger over a session
    holding the user's keys, so a script that "just works" with no argument is a script that will
    eventually be pointed somewhere nobody intended;
  * it must exit 2 — "could not run" — when it cannot reach the shell, never 0. checkall reports 2
    as a SKIP with its reason; a 0 there would read as "the desktop is fine", which is exactly the
    false green this whole investigation has been fighting.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/probe_shell.py"


def run(*args, timeout=60):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          capture_output=True, text=True, timeout=timeout)


def test_it_refuses_to_run_without_an_explicit_endpoint():
    done = run()
    assert done.returncode != 0
    assert "endpoint" in (done.stderr + done.stdout).lower()


def test_no_endpoint_is_baked_in():
    text = SCRIPT.read_text(encoding="utf-8")
    assert 'required=True' in text, "the endpoint stopped being mandatory"
    for hardcoded in ("192.168.", "poster.place", "default="):
        assert f'"--endpoint", {hardcoded}' not in text
    assert "9222" in text, "the docstring should still say how to arm it"


def test_an_unreachable_shell_is_could_not_run_not_success():
    """Port 1 is reserved and nothing listens there — the closest thing to a guaranteed refusal."""
    done = run("--endpoint", "http://127.0.0.1:1", timeout=90)
    assert done.returncode == 2, (done.returncode, done.stdout, done.stderr)
    assert "could not reach" in done.stderr


def test_it_says_how_to_arm_the_port_when_it_fails():
    """The failure a person will actually hit is 'I forgot the flag', so the error has to carry the
    answer rather than making them find this file."""
    done = run("--endpoint", "http://127.0.0.1:1", timeout=90)
    assert "PC_SHELL_EXTRA_ARGS" in done.stderr


def test_the_probe_expression_is_one_round_trip():
    """It is run against a desktop that is misbehaving; a chatty probe is a probe that hangs."""
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.count("Runtime.evaluate") == 1
