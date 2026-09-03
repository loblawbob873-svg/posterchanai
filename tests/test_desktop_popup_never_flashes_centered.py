from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SWAY = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text()
MAIN = (ROOT / "desktop/main.js").read_text()
WM = (ROOT / "desktop/wm.js").read_text()
PRELOAD = (ROOT / "desktop/preload.js").read_text()


def test_popup_maps_invisible_until_wayland_position_is_known():
    rule = next(line for line in SWAY.splitlines() if 'title="^PosterChan Popup$"' in line)
    assert "opacity set 0" in rule


def test_popup_geometry_and_reveal_are_one_compositor_transaction():
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
    assert placement.count("pc:host:popup-placed") == 2  # positioned success plus non-Sway fallback
