"""Startup Apps' offline fallback is armed only in the PosterChanOS shell.

It was a 15-second timer armed when os.js loaded, everywhere: a web page and every node test that
loads os.js waited 15s for nothing (test_desktop_widgets went from seconds to over five minutes and
a targeted run looked hung). In the shell (`window.pcShell`, from the desktop preload) it must still
be armed, or a machine whose account preferences never arrive never opens its startup apps."""
import shutil
import subprocess
import time
from pathlib import Path

import pytest

OS_JS = Path(__file__).resolve().parents[1] / "static" / "js" / "client" / "os.js"
BOOT = """
global.window = { addEventListener(){} %s };
global.document = { addEventListener(){}, querySelector(){ return null; }, querySelectorAll(){ return []; } };
global.getComputedStyle = () => ({ zoom: '1' });
const delays = [];
const real = setTimeout;
global.setTimeout = (fn, ms, ...a) => { delays.push(ms); return real(() => {}, 0); };
require(%r);
console.log(JSON.stringify(delays));
"""

pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node not installed")


def _load(shell):
    src = BOOT % (", pcShell: {}" if shell else "", str(OS_JS))
    t = time.monotonic()
    out = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-1500:]
    return time.monotonic() - t, out.stdout.strip().splitlines()[-1]


def test_a_page_outside_the_shell_arms_no_startup_timer_and_loads_at_once():
    took, delays = _load(shell=False)
    assert "15000" not in delays, delays
    # And it really exits: with the timer it took 15s.
    t = time.monotonic()
    subprocess.run(["node", "-e", "global.window={addEventListener(){}};global.document={addEventListener(){},"
                    "querySelector(){return null},querySelectorAll(){return []}};global.getComputedStyle=()=>({zoom:'1'});"
                    "require(%r)" % str(OS_JS)], capture_output=True, timeout=60)
    assert time.monotonic() - t < 8, "os.js keeps node alive after loading"


def test_the_shell_still_arms_the_offline_fallback():
    _took, delays = _load(shell=True)
    assert "15000" in delays, delays
