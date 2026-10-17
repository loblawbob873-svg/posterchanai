"""When the desktop app's GPU process dies, the log says which windows were open and which had focus.

2026-10-17, RX 6700 desktop: an amdgpu page fault in the app's GPU process hung the GPU for 21 s ("desktop
posterchan froze for a few seconds"); the shell log said only "GPU process exited unexpectedly". Runs the
SHIPPED child-process-gone handler from desktop/main.js with a stand-in for Electron's window list.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node required")
def test_a_gpu_death_names_the_windows():
    src = (ROOT / "desktop/main.js").read_text()
    m = re.search(r"app\.on\('child-process-gone', (\(_e, d\) => \{.*?\n\})\);", src, re.S)
    assert m, "the child-process-gone handler moved"
    js = """
const logs=[]; const console={warn:(...a)=>logs.push(a.join(' '))};
const _childDeaths=[];
const mk=(t,vis)=>({getTitle:()=>t,isVisible:()=>vis,isDestroyed:()=>false});
const term=mk('Terminal',true), social=mk('Social',true), pop=mk('PosterChan Popup',false);
const BrowserWindow={getFocusedWindow:()=>term,getAllWindows:()=>[social,term,pop]};
const h=%s;
h(null,{type:'GPU',reason:'crashed',exitCode:133});
h(null,{type:'Utility',reason:'crashed',exitCode:1});
process.stdout.write(JSON.stringify(logs));
""" % m.group(1)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    logs = json.loads(out.stdout)
    named = [l for l in logs if "gpu died with windows" in l]
    assert len(named) == 1, logs
    assert '"*Terminal"' in named[0] and '"Social"' in named[0] and "PosterChan Popup (hidden)" in named[0], named
