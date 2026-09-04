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


def test_the_shield_has_a_floor_because_the_latch_is_set_before_the_attempt():
    """A LIVE, FULLY PAINTED MENU AT opacity:0 IS INDISTINGUISHABLE FROM A DEAD BUTTON.

    The sheet is lifted only by `pc:host:popup-placed`, and `placePopupWindow` has three ways of
    never sending it: the window was destroyed, `_popupWin !== win` (a second popup replaced this
    one mid-flight), or the send itself throws. Observed on the real desktop while chasing the tray
    flyout: a mapped `PosterChan Popup` view of the right size, in the right place, with `.os-pop`
    drawn inside it — and nothing on screen.

    Placement's own worst case is bounded (12 attempts 60ms apart, then it sends regardless), so
    anything past a second means the message is not coming. Revealing an unplaced menu shows it
    briefly where the compositor put it — the flash this shield exists to prevent — but a flash is a
    menu you can use, and this is the case where the alternative is none.
    """
    body = PRELOAD.split("--pc-popup-surface", 1)[1].split("\n}\n", 1)[0]
    assert "setTimeout(" in body, body
    floor = int(body.split("setTimeout(()=>reveal(", 1)[1].split("),", 1)[1].split(")", 1)[0])
    # Longer than placement's worst case (12 * 60ms) so it can never pre-empt a real placement…
    assert floor > 720, floor
    # …and short enough that nobody experiences it as a dead control.
    assert floor <= 3000, floor
    # One reveal path, so a placement that arrives after the floor cannot re-insert or double-remove.
    assert body.count("removeInsertedCSS") == 2, body
    assert "if(placed) return;" in body
