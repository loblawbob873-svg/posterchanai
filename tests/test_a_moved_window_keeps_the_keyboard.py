"""Super+Shift+Arrow moves a window, and it is still the window being typed into.

Measured on a two-output Wayfire session: a popped-out Terminal moved to the other monitor arrived
unfocused and the desktop surface it left took the keyboard. After the fix, the Terminal (through
pc-window-snap) and Telegram (through the main-process move-to-output route) both arrived focused.
The helper runs for real with only the compositor IPC stubbed.
"""
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SNAP = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-window-snap"

OUTPUTS = [{"id": 1, "name": "HEADLESS-2", "geometry": {"x": 0, "y": 0, "width": 1280, "height": 720},
            "workarea": {"x": 0, "y": 0, "width": 1280, "height": 720}},
           {"id": 3, "name": "HEADLESS-1", "geometry": {"x": 1280, "y": 0, "width": 1280, "height": 720},
            "workarea": {"x": 0, "y": 0, "width": 1280, "height": 720}}]
TERM = {"id": 9, "app-id": "place.poster.desktop", "title": "PosterChan Window — terminal",
        "output-id": 1, "pid": 4242, "geometry": {"x": 258, "y": 68, "width": 763, "height": 581}}


def test_a_popped_out_window_moved_to_another_monitor_is_focused_there():
    module = runpy.run_path(str(SNAP), run_name="pc_window_snap_move")
    ipc = []

    def fake_wayfire(method, data=None):
        if method == "window-rules/list-views":
            return [dict(TERM, activated=True, mapped=True)]
        if method == "window-rules/list-outputs":
            return OUTPUTS
        ipc.append((method, data))
        return {}

    g = module["wayfire_main"].__globals__
    g["wayfire"] = fake_wayfire
    g["shell_action"] = lambda *_: None
    g["is_ours"] = lambda win: True
    module["wayfire_main"]("move-right")
    methods = [m for m, _ in ipc]
    assert methods[:1] == ["window-rules/configure-view"] and ipc[0][1]["output_id"] == 3, ipc
    assert methods[-1] == "window-rules/focus-view" and ipc[-1][1] == {"id": 9}, ipc


def test_the_native_route_focuses_after_placing():
    main = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
    route = main[main.index("ipcMain.handle('pc:wm:move-to-output'"):main.index("ipcMain.handle('pc:wm:handoff'")]
    assert "wm().focus(nativeId)" in route
    assert route.index("placeOnOutput(") < route.index("wm().focus(nativeId)")
