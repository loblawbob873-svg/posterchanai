"""Startup apps pinned to a monitor open THERE, once, and that monitor tiles them.

"if you can add to OS Settings Startup Apps a way to pin startup apps to a specific monitor and apply a tiling
style" (2026-10-08). Every monitor runs its own desktop renderer and a window opens on its opener's monitor, so
os.js _startupPlan decides, per renderer, which apps it opens and how it tiles. Runs the SHIPPED function under
node. The rules that matter are the ones that would show up as a doubled or a missing window: every app opens on
exactly one monitor, and a pin to a monitor this machine does not have falls back to the main one.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

OS = Path(__file__).resolve().parents[1] / "static" / "js" / "client" / "os.js"


def _fn():
    src = OS.read_text()
    a = src.index("  function _startupPlan(")
    b = src.index("\n  }\n", a) + 4
    return src[a:b]


def plan(cases):
    if not shutil.which("node"):
        pytest.skip("node not installed")
    js = _fn() + "\nconst c=JSON.parse(process.argv[1]);process.stdout.write(JSON.stringify(c.map(x=>_startupPlan(...x))));"
    r = subprocess.run(["node", "-e", js, json.dumps(cases)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


VIEWS = ["messages", "notes", "concord", "music"]
PLACE = {"concord": "HDMI-A-1", "music": "DP-2", "notes": ""}
TILE = {"DP-1": "grid", "HDMI-A-1": "side-by-side"}
MAIN = {"output": "DP-1", "owner": True, "present": ["DP-1", "HDMI-A-1"]}
SIDE = {"output": "HDMI-A-1", "owner": False, "present": ["DP-1", "HDMI-A-1"]}


def test_each_app_opens_on_exactly_one_monitor():
    main, side = plan([[VIEWS, PLACE, TILE, MAIN], [VIEWS, PLACE, TILE, SIDE]])
    # music is pinned to DP-2, which this machine does not have: it opens on the main monitor.
    assert main["open"] == ["messages", "notes", "music"], main
    assert side["open"] == ["concord"], side
    assert sorted(main["open"] + side["open"]) == sorted(VIEWS), "an app opened twice, or not at all"


def test_each_monitor_tiles_its_own_apps_with_its_own_style():
    main, side = plan([[VIEWS, PLACE, TILE, MAIN], [VIEWS, PLACE, TILE, SIDE]])
    assert main["layout"] == "grid" and side["layout"] == "side-by-side"


def test_nothing_to_open_means_nothing_to_tile_and_junk_styles_are_ignored():
    empty, junk = plan([[["messages"], {"messages": "DP-1"}, TILE, SIDE],
                        [["messages"], {}, {"DP-1": "spiral"}, MAIN]])
    assert empty == {"open": [], "layout": ""}, "a monitor with no startup apps re-arranged whatever was there"
    assert junk == {"open": ["messages"], "layout": ""}


def test_with_no_layout_settings_it_behaves_exactly_as_before():
    # No placement and no tiling: the main monitor opens everything, the others nothing, nothing is tiled.
    main, side = plan([[VIEWS, {}, {}, MAIN], [VIEWS, {}, {}, SIDE]])
    assert main == {"open": VIEWS, "layout": ""} and side == {"open": [], "layout": ""}


def test_the_desktop_tells_each_renderer_which_monitor_it_is():
    root = Path(__file__).resolve().parents[1] / "desktop"
    assert "outputName: () => ipcRenderer.invoke('pc:shell:output')" in (root / "preload.js").read_text()
    main = (root / "main.js").read_text()
    i = main.index("ipcMain.handle('pc:shell:output'")
    assert "_shellScopes.get(e.sender.id)" in main[i:i + 200]


def test_monitor_and_tiling_choices_never_travel_with_the_account():
    """Per device: a laptop signed into the same account must not inherit a desktop's monitor names."""
    root = Path(__file__).resolve().parents[1] / "static" / "js" / "client"
    for f in ("app.js", "blossom.js"):
        src = (root / f).read_text()
        assert "startupPlacement" not in src and "startupTiling" not in src, f
