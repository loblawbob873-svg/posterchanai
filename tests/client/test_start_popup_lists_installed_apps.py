from pathlib import Path


SRC = (Path(__file__).resolve().parents[2] / "static/js/client/os.js").read_text()


def test_start_popup_detects_its_own_compositor_before_building_the_menu():
    """Popup renderers have independent JS state; availability proven in the desktop renderer
    cannot make installed applications visible in the popup renderer."""
    start = SRC.index("function renderStartPopup()")
    end = SRC.index("function renderNotiPopup()", start)
    body = SRC[start:end]
    assert "Promise.resolve(PCOSShell.detect()).then" in body
    assert "if(!host.isConnected) return" in body
    assert body.count("toggleStart(true)") >= 2


def test_popup_restore_keeps_the_async_detection_alive():
    restore = SRC[SRC.index("function restore()"):]
    assert "if(k === 'start') renderStartPopup();" in restore
