"""THE WIZARD MUST NEVER BE A DEAD END, and the network step was one.

Reported from an ethernet-only desktop, freshly installed, whose NIC NetworkManager did not bring
up: "no wifi listed", then "i have no access to the machine". The wizard covers the desktop and
there is no terminal in front of it, so a machine NM cannot get online was held at that screen for
ever.

machineUnusable() returns true while the network step is unfinished, and network was the ONLY step
with no answer meaning "not now" — instance, tor and signin all have one. The step that can lock
the door was the one that could not be answered.

A skip is deliberately NOT a fake `online`: it records that somebody was asked and chose to go on,
so every later screen stays honest about what it can reach.
"""
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "static" / "js" / "client" / "osfirstrun.js"

DRIVER = """
const {readFileSync} = require('fs');
globalThis.window = globalThis;
eval(readFileSync(%s, 'utf8'));
const FR = globalThis.window.PCFirstRun || globalThis.PCFirstRun;
const base = {netReadable:true, online:false, instanceSkipped:true, torSkipped:true,
              signinSkipped:true, everHadAccount:true};
console.log(JSON.stringify({
  offline_blocks:       FR.machineUnusable({...base}),
  offline_skipped_ok:  !FR.machineUnusable({...base, networkSkipped:true}),
  state_offline:        FR.stepState({...base}).network,
  state_skipped:        FR.stepState({...base, networkSkipped:true}).network,
  state_online:         FR.stepState({...base, online:true}).network,
  nm_dead:              FR.stepState({netReadable:false}).network,
}));
""" % json.dumps(str(SRC))


@pytest.fixture(scope="module")
def result():
    if not SRC.exists():
        pytest.skip("osfirstrun.js is not present")
    try:
        done = subprocess.run(["node", "-e", DRIVER], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        pytest.skip("node is not installed")
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout)


def test_an_offline_machine_is_still_asked(result):
    """The skip must not fire on its own — a machine that CAN get online should be helped to."""
    assert result["offline_blocks"] is True
    assert result["state_offline"] == "todo"


def test_an_offline_machine_can_be_let_through(result):
    """The whole point: ethernet-only hardware NM has not brought up must not be bricked."""
    assert result["offline_skipped_ok"] is True
    assert result["state_skipped"] == "done"


def test_being_online_still_passes_by_itself(result):
    """A machine on a cable has nothing to ask, and asking anyway is the wizard being pleased."""
    assert result["state_online"] == "done"


def test_a_dead_network_manager_is_still_its_own_answer(result):
    """`blocked` is not `todo`: picking a network cannot fix a NetworkManager that is not running,
    and collapsing the two would hide it behind a skip button."""
    assert result["nm_dead"] == "blocked"


def test_the_button_exists_in_the_shipped_ui():
    ui = (ROOT / "static" / "js" / "client" / "osfirstrunui.js").read_text(encoding="utf-8")
    assert 'data-fr="nonet"' in ui, "the network step has no way past it"
    assert "pc_fr_network_skipped" in ui, "the choice is not recorded, so it is asked again"
