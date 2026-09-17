"""A file chooser opens above the window that asked for it.

"System Settings -> Build ISO -> clicking opens the file picker behind System Settings." Every picker
was parented to `win`, the desktop surface, which this shell keeps below applications; System
Settings is its own toplevel, so its dialog was stacked with the desktop, underneath it.

Every dialog opened from an IPC handler must take its parent from the SENDER (`dialogOwner(e)`), and
`dialogOwner` is run under node against the three cases that matter.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop/main.js").read_text(encoding="utf-8")


def _handlers():
    """(channel, body) for every ipcMain.handle block, split on the next registration."""
    starts = [m.start() for m in re.finditer(r"ipcMain\.(?:handle|on)\(", MAIN)]
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(MAIN)
        block = MAIN[s:end]
        ch = re.match(r"ipcMain\.(?:handle|on)\('([^']+)'", block)
        yield (ch.group(1) if ch else "?"), block


def test_every_chooser_opened_for_a_page_is_parented_to_that_page():
    found = []
    for channel, block in _handlers():
        for m in re.finditer(r"dialog\.show(?:Open|Save)Dialog\(\s*([A-Za-z_]+(?:\(e\))?)\s*,", block):
            found.append((channel, m.group(1).strip()))
    assert len(found) >= 6, found
    wrong = [(c, a) for c, a in found if a != "dialogOwner(e)"]
    assert not wrong, "these pickers open under the desktop, behind their own window: %s" % wrong
    assert "pc:liveusb:pick-dir" in {c for c, _ in found}


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_the_owner_is_the_senders_window_and_the_desktop_only_as_a_fallback():
    start = MAIN.index("const dialogOwner = (e) => {")
    end = MAIN.index("};", start) + 2
    fn = MAIN[start:end]
    script = """
const win={name:'desktop',isDestroyed:()=>false};
const settings={name:'settings',isDestroyed:()=>false};
const closed={name:'closed',isDestroyed:()=>true};
const map=new Map([['s',settings],['c',closed]]);
const BrowserWindow={fromWebContents:(wc)=>map.get(wc)||null};
%s
process.stdout.write(JSON.stringify([
  dialogOwner({sender:'s'}).name, dialogOwner({sender:'c'}).name,
  dialogOwner({sender:'nobody'}).name, dialogOwner(null).name]));
""" % fn
    got = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert got == ["settings", "desktop", "desktop", "desktop"]
