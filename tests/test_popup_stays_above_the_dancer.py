"""A tray popup is ABOVE PosterChan, not under her.

The live USB, 2026-10-29: "Posterchan is dancing ... covering the choose wifi network". Her window is kept
always-on-top through the compositor (buddy-host.js), which puts her in Wayfire's always-on-top layer; the
popup was only focused, and focus raises a window within its own layer -- so the Quick Settings flyout, which
opens in the same bottom-right corner she stands in by default, stayed under her. The shipped
placePopupWindow, cut out of main.js and run under node with a recording window manager: the popup joins the
always-on-top layer, and is focused AFTER that so it is in front within it.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAIN = (ROOT / "desktop/main.js").read_text()
NODE = shutil.which("node")


def _fn(name):
    a = MAIN.index(f"async function {name}(")
    return MAIN[a:MAIN.index("\n}\n", a) + 3]


@pytest.mark.skipif(not NODE, reason="node required")
def test_the_popup_joins_the_always_on_top_layer_then_takes_the_front():
    js = r"""
const calls = [];
const POPUP_TITLE = 'PosterChan Popup';
const win = { isDestroyed: () => false, getBounds: () => ({ width: 360, height: 420 }), setSize(){},
              webContents: { send: (ch) => calls.push(['send', ch]) } };
let _popupWin = win, _popupKind = 'tray';
const snapPopupToWorkArea = (w) => ({ x: w.x, y: w.y, w: w.w, h: w.h });
const W = {
  windows: async () => [{ id: 7, title: 'PosterChan Buddy', rect: { x: 1752, y: 778, width: 168, height: 265 } },
                        { id: 9, title: POPUP_TITLE, rect: { x: 0, y: 0, width: 360, height: 420 } }],
  outputs: async () => [],
  placeAndReveal: async (id) => calls.push(['place', id]),
  alwaysOnTop: async (id, on) => calls.push(['top', id, on]),
  focus: async (id) => calls.push(['focus', id]),
};
const wm = () => W;
const setTimeout = (f) => f();
%s
placePopupWindow(win, { x: 1540, y: 600, w: 360, h: 420 }).then(() => console.log(JSON.stringify(calls)));
""" % _fn("placePopupWindow")
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    calls = json.loads(r.stdout.strip().splitlines()[-1])
    names = [c[0] for c in calls]
    assert ["top", 9, True] in calls, ("the popup was never put above always-on-top windows", calls)
    assert names.index("top") < names.index("focus"), ("focused before joining the layer", calls)
    assert ["top", 7, True] not in calls and all(c[1] == 9 for c in calls if c[0] in ("top", "focus")), calls
