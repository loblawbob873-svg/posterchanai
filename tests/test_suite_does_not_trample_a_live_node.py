"""THE TEST SUITE MUST NOT DEGRADE THE NODE IT IS RUNNING ON.

`./test.sh` fans out ~20 cold headless Chrome sessions. Half the cores is a fine default on a dev
machine and the wrong one on a deploy node — which is the commonest place it gets run, because that
is where the code is.

Measured on server1 while the suite ran: load 3.04, and the relay's newest event two minutes stale.
Reported from a phone, at the same moment, as "no posts coming in" and "even on lan, 1 min behind".
The suite that exists to check the system was the thing degrading it, and nothing in its output
said so.

So it notices whether this box is serving the app and the relay, halves itself, and prints why.
`--jobs N` still wins for anyone who means it — including CI, where nothing is being served and the
full width is correct.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import checkall  # noqa: E402


def test_a_serving_node_runs_fewer_browsers(monkeypatch):
    """THE FIX. On a box that is serving, the fan-out is capped low."""
    monkeypatch.setattr(checkall, "_serving_live_traffic", lambda: True)
    assert checkall._default_jobs() <= 2


def test_a_quiet_machine_still_uses_the_cores_it_has(monkeypatch):
    """The throttle must not become the default everywhere — CI has nothing to protect and a
    ten-minute suite that takes forty is a suite people stop running."""
    monkeypatch.setattr(checkall, "_serving_live_traffic", lambda: False)
    monkeypatch.setattr(checkall.os, "cpu_count", lambda: 16)
    assert checkall._default_jobs() > 2


def test_an_explicit_jobs_flag_always_wins():
    """A person who passes --jobs means it; the throttle is a default, not a policy."""
    src = (ROOT / "scripts/checkall.py").read_text(encoding="utf-8")
    assert "args.jobs or _default_jobs()" in src, (
        "--jobs no longer takes precedence over the automatic throttle")


def test_the_throttle_says_so_out_loud():
    """A suite that silently runs at a third of its speed reads as a suite that got slower."""
    src = (ROOT / "scripts/checkall.py").read_text(encoding="utf-8")
    assert "serving live traffic" in src and "--jobs N overrides" in src


def test_detection_never_throws():
    """systemctl may be absent (a container, a mac, CI). Failing to detect must mean 'not serving',
    never an exception that takes the whole run down before a single check has been made."""
    assert checkall._serving_live_traffic() in (True, False)
