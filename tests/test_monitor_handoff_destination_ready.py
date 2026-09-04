"""Monitor handoff must not send state into a renderer that is reloading or in Classic mode."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
PRELOAD = (ROOT / "desktop" / "preload.js").read_text(encoding="utf-8")
CLIENT = (ROOT / "static" / "js" / "client" / "os.js").read_text(encoding="utf-8")


def _handler(name, next_name):
    start = MAIN.index("ipcMain.handle('" + name + "'")
    return MAIN[start:MAIN.index("ipcMain.handle('" + next_name + "'", start)]


def test_preload_exposes_an_explicit_destination_readiness_handshake():
    assert "handoffReady: (ready) => ipcRenderer.invoke('pc:wm:handoff-ready'" in PRELOAD
    assert "ipcMain.handle('pc:wm:handoff-ready'" in MAIN


def test_native_handoff_refuses_before_moving_any_surface_when_destination_is_not_ready():
    body = _handler("pc:wm:handoff", "pc:wm:handoff-frame")
    guard = body.index("_handoffReady.has")
    assert guard < body.index("finishMove(")
    assert guard < body.index("moveToAssignment(")
    assert "isLoadingMainFrame()" in body


def test_dom_handoff_refuses_before_sending_or_focusing_when_destination_is_not_ready():
    body = _handler("pc:wm:handoff-frame", "pc:wm:preview-frame")
    guard = body.index("_handoffReady.has")
    assert guard < body.index("webContents.send('pc:wm:handoff-frame'")
    assert guard < body.index("wm().focus")
    assert "isLoadingMainFrame()" in body


def test_navigation_invalidates_old_readiness_for_the_same_webcontents_id():
    assert "created.webContents.on('did-start-navigation'" in MAIN
    assert "if(isMainFrame) _handoffReady.delete" in MAIN
    assert "_handoffReady.delete(contentsId)" in MAIN


def test_renderer_is_ready_only_after_all_destination_listeners_are_installed():
    ready = CLIENT.index("pcWM.handoffReady(true)")
    for listener in ("pcWM.onNativeHandoff", "pcWM.onHandoffFrame", "pcWM.onPreviewFrame"):
        assert CLIENT.index(listener) < ready
    exit_start = CLIENT.index("function exit(remember)")
    exit_end = CLIENT.index("function toggle", exit_start)
    assert "pcWM.handoffReady(false)" in CLIENT[exit_start:exit_end]
