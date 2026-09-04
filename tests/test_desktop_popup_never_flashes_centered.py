from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WAYFIRE = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini").read_text()
MAIN = (ROOT / "desktop/main.js").read_text()
WM = (ROOT / "desktop/wm.js").read_text()
PRELOAD = (ROOT / "desktop/preload.js").read_text()


def test_popup_maps_invisible_until_wayland_position_is_known():
    """THE COMPOSITOR HALF OF THIS IS GONE, AND THE RENDERER HALF IS WHY THAT IS SURVIVABLE.

    Sway mapped this title at `opacity set 0` and `placeAndReveal` set it back to 1 in the SAME
    command as the geometry, so the surface was never painted anywhere but its final position.
    Wayfire 0.10 has no equivalent per-window opacity rule, so the outer safety net is gone -- what
    remains is the renderer-side shield, which is compositor-neutral and was always the inner one:
    the BrowserWindow is opened `transparent`, preload inserts `html,body{opacity:0}` before first
    paint, and main.js only sends `pc:host:popup-placed` after the window has been positioned.

    That is asserted in full by the two tests below; this one only pins that the flyout is NOT
    decorated by the compositor, because a server-side title bar would be drawn at the centre
    placement regardless of what the page inside it is doing.
    """
    assert 'title contains "PosterChan Window"' in WAYFIRE
    ignore = next(line for line in WAYFIRE.splitlines() if line.startswith("ignore_views"))
    assert "PosterChan" in ignore


def test_popup_geometry_and_reveal_are_one_compositor_transaction():
    """Sway's backend still commits geometry and reveal together; it is kept and still tested."""
    body = WM.split("async placeAndReveal(", 1)[1].split("async placeOnOutput(", 1)[0]
    command = body.split("return this.command(", 1)[1]
    assert "resize set" in command
    assert "move absolute position" in command
    assert "opacity set 1" in command
    assert command.count("this.command(") == 0


def test_popup_main_path_uses_atomic_reveal_not_plain_place():
    body = MAIN.split("async function placePopupWindow", 1)[1].split(
        "ipcMain.handle('pc:popup:close'", 1
    )[0]
    assert "placeAndReveal" in body
    assert ".place(Number(row.id)" not in body


def test_popup_renderer_stays_transparent_until_placement_acknowledgement():
    create = MAIN.split("const p = new BrowserWindow({", 1)[1].split("});", 1)[0]
    assert "transparent: true" in create
    assert "#00000000" in create
    assert "--pc-popup-surface" in create
    placement = MAIN.split("async function placePopupWindow", 1)[1].split(
        "ipcMain.handle('pc:popup:close'", 1
    )[0]
    assert placement.index("placeAndReveal") < placement.index("pc:host:popup-placed")
    assert "webFrame.insertCSS" in PRELOAD
    assert "opacity:0!important" in PRELOAD
    assert "ipcRenderer.once('pc:host:popup-placed'" in PRELOAD
    assert "webFrame.removeInsertedCSS" in PRELOAD
    assert placement.count("pc:host:popup-placed") == 2  # positioned success plus the no-compositor fallback
