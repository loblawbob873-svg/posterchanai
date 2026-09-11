"""FOLDER SYNC MUST FIND THE ONE WRITER, EVEN FROM THE FAR SIDE OF A SECOND MONITOR.

`FS()` decides which bridge this screen may use. The rule it enforces is real and stays: ONE writer
per device, and that writer is the PRIMARY surface, because that is the surface that sweeps. A
secondary window therefore borrows the primary's bridge (same-origin children share the opener's
process and objects), and with no primary in reach it returns null — which renders "Set up on this
device…" DISABLED rather than offering a control that cannot work.

IT ONLY EVER LOOKED ONE HOP. On a multi-monitor PosterChanOS the windows on the second screen are
opened from a window that is itself a secondary surface, so the chain is

    this window  →  a secondary  →  the primary

and a single check found a secondary at the first hop and gave up. Folder Sync's setup button was
disabled on every screen but one, silently, because a disabled button explains nothing — the same
shape as the report that produced this function in the first place.

These run the SHIPPED `FS()` against fake window chains: the primary, one hop, two hops, a closed
window, a cycle, and a chain with no primary at all (which must still refuse).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")
SYNC = (ROOT / "static/js/client/sync.js").read_text(encoding="utf-8")


def _fs_source() -> str:
    """The shipped FS() and the hop bound it uses."""
    start = SYNC.index("  const FS_OPENER_HOPS")
    end = SYNC.index("/* Sizes for the humans reading this screen.", start)
    return SYNC[start:end]


def run(chain: str) -> str | None:
    """`chain` builds `window`; returns which bridge FS() handed back."""
    program = """
      %s
      %s
      const got = FS();
      console.log(JSON.stringify(got ? got.name : null));
    """ % (chain, _fs_source())
    done = subprocess.run([NODE, "-e", program], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-1500:]
    return json.loads(done.stdout.strip())


PRIMARY = "{pcFs:{name:'primary'},pcShell:{backgroundOwner:true},closed:false}"


def _secondary(opener, name="secondary"):
    return ("{pcFs:{name:'%s'},pcShell:{backgroundOwner:false},closed:false,opener:%s}"
            % (name, opener))


@pytest.mark.skipif(not NODE, reason="needs node")
def test_the_primary_surface_uses_its_own_bridge():
    assert run(f"globalThis.window = {PRIMARY};") == "primary"


@pytest.mark.skipif(not NODE, reason="needs node")
def test_one_hop_borrows_the_primary():
    assert run(f"globalThis.window = {_secondary(PRIMARY)};") == "primary"


@pytest.mark.skipif(not NODE, reason="needs node")
def test_two_hops_still_finds_it():
    """THE MULTI-MONITOR CASE. A window opened from a window that is itself secondary."""
    middle = _secondary(PRIMARY, "middle")
    assert run(f"globalThis.window = {_secondary(middle, 'far-screen')};") == "primary", (
        "a window two hops from the primary could not find it, so Folder Sync's setup button is "
        "disabled on every screen but one")


@pytest.mark.skipif(not NODE, reason="needs node")
def test_a_chain_with_no_primary_still_refuses():
    """The rule is not 'find any bridge' — it is 'find the WRITER'. Two writers on one device is
    the thing this whole function exists to prevent."""
    deep = _secondary(_secondary(_secondary("null", "c"), "b"), "a")
    assert run(f"globalThis.window = {deep};") is None


@pytest.mark.skipif(not NODE, reason="needs node")
def test_a_closed_window_is_not_followed():
    closed = "{pcFs:{name:'gone'},pcShell:{backgroundOwner:false},closed:true,opener:%s}" % PRIMARY
    assert run(f"globalThis.window = {_secondary(closed)};") is None


@pytest.mark.skipif(not NODE, reason="needs node")
def test_a_cycle_does_not_hang_the_screen():
    """`opener` can point back at a window already walked. An unbounded walk would spin while
    drawing the screen it is meant to draw."""
    build = """
      const a = {pcFs:{name:'a'},pcShell:{backgroundOwner:false},closed:false};
      const b = {pcFs:{name:'b'},pcShell:{backgroundOwner:false},closed:false,opener:a};
      a.opener = b;
      globalThis.window = {pcFs:{name:'me'},pcShell:{backgroundOwner:false},closed:false,opener:a};
    """
    assert run(build) is None


def test_the_walk_stays_bounded():
    """A number, present and small. Without it the cycle test above is the only thing standing
    between a self-referencing opener and a frozen tab."""
    src = _fs_source()
    assert "FS_OPENER_HOPS" in src
    assert "hop < FS_OPENER_HOPS" in src, "the opener walk is no longer bounded"
