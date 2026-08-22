from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_dragging_stashes_native_surface_and_places_it_once():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    drag = src[src.index("function startDrag"):src.index("function startResize")]
    gesture = src[src.index("function _natGesture"):src.index("const _zOf")]
    assert "pcWM.hide(w.native)" in gesture
    assert "_natMove(w)" not in drag
    assert "pcWM.place" not in drag
    assert "setPointerCapture(ev.pointerId)" in drag
    assert "if(w.native == null) window.addEventListener('blur', up)" in drag


def test_native_bridge_retains_move_for_non_gesture_placement_operations():
    preload = (ROOT / "desktop/preload.js").read_text(encoding="utf-8")
    main = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
    wm = (ROOT / "desktop/wm.js").read_text(encoding="utf-8")
    assert "pc:wm:move" in preload and "pc:wm:move" in main
    assert "move(id, x, y)" in wm


def test_taskbar_is_icon_only():
    css = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
    assert ".os-task span{display:none}" in css
    assert ".os-task .ic{width:20px;height:20px" in css


def test_native_task_buttons_have_an_existing_fallback_icon():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    assert "'#i-grid'" in src
    assert "'App', 'i-grid'" in src


def test_native_apps_inherit_the_dark_gtk_chrome():
    for path in (
        ROOT / "os/bin/pc-shell-start",
        ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-shell-start",
    ):
        start = path.read_text(encoding="utf-8")
        assert 'GTK_THEME="${GTK_THEME:-Adwaita:dark}"' in start
        assert "GTK_APPLICATION_PREFER_DARK_THEME=1" in start
