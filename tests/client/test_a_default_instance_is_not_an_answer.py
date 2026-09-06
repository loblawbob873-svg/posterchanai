"""EVERY LiveISO SHIPPED POINTED AT THE DEVELOPER'S INSTANCE, AND NEVER ASKED.

Measured on a freshly built image, from the guest's own console:

    [firstrun] showing step=tor blocked=0
    state={"network":"done","instance":"done","tor":"todo","signin":"todo","account":"todo"}

`instance` was already `done` on a machine that had never been configured. `desktop/main.js`
answers `instance()` with `DEFAULT_INSTANCE` whenever nothing is set -- right for a downloaded
Windows or macOS build, where somebody who installed the app wants it to work without being
interrogated first, and wrong for a machine booting PosterChanOS for the first time, where "which
instance?" is a question the wizard exists to ask. Both `apiBase()` and `__PC_API_BASE__` therefore
come back full on a completely fresh install, and the wizard read that as an answer.

So a new user silently adopted somebody else's server and was never offered the choice -- on an
image whose entire clean-out exists so that a disc carries nothing of the machine that built it.

The shipped world-building RUNS here. A source-text assertion cannot answer this: the old code and
the new code both mention `__PC_API_BASE__`, and the difference is only what they conclude from it.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tests/client/firstrun_instance_sim.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _instance(**plan):
    out = subprocess.run(["node", str(SIM), json.dumps(plan)],
                         cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr[:2000]
    return json.loads(out.stdout)["instance"]


def test_a_fresh_machine_is_asked_which_instance():
    """The whole bug. The shell reports a URL because it has a default, not because anybody chose."""
    assert _instance(shell=True, chosen=False, apiBase="https://poster.place", bundled=True) is False


def test_an_instance_somebody_chose_is_not_asked_again():
    assert _instance(shell=True, chosen=True, apiBase="https://mine.example", bundled=True) is True


def test_the_web_client_still_counts_the_instance_serving_it():
    """Only the shell can tell a default from a choice. Everywhere else a base that is present IS a
    real choice, and treating it as unanswered would put a wizard in front of a working web app."""
    assert _instance(shell=False, apiBase="https://served.example", bundled=False) is True


def test_an_older_shell_without_the_flag_falls_back_to_the_old_test():
    """A bundle newer than its main process must not decide the machine has no instance."""
    assert _instance(shell=False, apiBase="https://poster.place", bundled=True) is True


def test_no_instance_anywhere_is_still_unanswered():
    assert _instance(shell=False, apiBase="", bundled=True) is False
