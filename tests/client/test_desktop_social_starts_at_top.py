from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OS = (ROOT / "static/js/client/os.js").read_text()
APP = (ROOT / "static/js/client/app.js").read_text()


def test_new_social_window_uses_authoritative_refresh_and_top_latch():
    create = OS.split("function openApp(", 1)[1].split("return w;", 1)[0]
    assert "(view==='home'||view==='global')" in create
    assert "PC().timelineTop(view)" in create
    assert "timelineTop," in APP.split("window.__PC =", 1)[1]


def test_social_launcher_identity_is_covered_by_the_fresh_window_latch():
    # apps() reads the real sidebar data-view; Social is Nostrverse/global, not Home.
    assert "{ v:'global', label:'Social'" in APP
    create = OS.split("function openApp(", 1)[1].split("return w;", 1)[0]
    assert "view==='global'" in create


def test_existing_social_window_preserves_its_reading_position():
    start = OS.index("function openApp(")
    existing = OS.index("const existing = wins.find", start)
    force = OS.index("PC().timelineTop(view)", start)
    assert existing < force
    branch = OS[existing:OS.index("const app =", existing)]
    assert "focusWin(existing" in branch
    assert "timelineTop" not in branch
