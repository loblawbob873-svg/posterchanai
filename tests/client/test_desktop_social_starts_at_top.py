from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text()
APP = (ROOT / "static/js/client/app.js").read_text()


def test_new_social_window_uses_authoritative_refresh_and_top_latch():
    create = OS.split("function openApp(", 1)[1].split("return w;", 1)[0]
    assert "if(view==='home' && PC().timelineTop) PC().timelineTop('home')" in create
    assert "timelineTop," in APP.split("window.__PC =", 1)[1]


def test_existing_social_window_preserves_its_reading_position():
    start = OS.index("function openApp(")
    existing = OS.index("if(existing){ focusWin(existing); return existing; }", start)
    force = OS.index("PC().timelineTop('home')", start)
    assert existing < force
