from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_dragging_keeps_native_surface_live_and_coalesces_position_moves():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    drag = src[src.index("function startDrag"):src.index("function startResize")]
    assert "if(nativeWins().length) nsync()" in drag
    assert "if(stash.has(it.native))" in src
    assert "if(it.w.gesturing && was !== 'hidden')" in src
    assert "await pcWM.move(it.native, rect.x, rect.y)" in src
    # Sway refuses `floating enable`/resize on a hidden scratchpad container. Restore first, then
    # place; the opposite order leaves the native app parked while only its HTML frame moves.
    assert src.index("pcWM.show(it.native") < src.index("pcWM.place(it.native")
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


def test_snapping_ends_move_only_mode_before_the_full_native_resize():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    up = src[src.index("const up = (endEvent) =>", src.index("function startDrag")):
             src.index("document.addEventListener('pointermove'", src.index("function startDrag"))]
    assert up.index("_natGesture(w, false)") < up.index("if(zone) snapTo(w, zone)")
    snap = src[src.index("function snapTo"):src.index("function unsnap")]
    assert "_natSent.delete(Number(w.native))" in snap
    assert "requestAnimationFrame(() => requestAnimationFrame(nsync))" in snap


def test_taskbar_is_icon_only():
    css = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
    assert ".os-task span{display:none}" in css
    assert ".os-task .ic{width:20px;height:20px" in css


def test_native_task_buttons_have_an_existing_fallback_icon():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    assert "${appIcon(w)}" in src
    assert "(a && a.icon) || 'i-grid'" in src
    assert 'data-kind="native"' in src


def test_native_programs_are_adopted_once_into_real_posterchan_frames():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    native = src[src.index("function adoptNative(nw)"):src.index("async function adoptAll()")]
    assert "openApp(view" in native
    assert "w.native=id" in native
    assert "osw-native" in native
    adopt = src[src.index("async function adoptAll"):src.index("function closeWin(w, opts)")]
    assert "nativeTasks = rows" in adopt
    assert "adoptNative(r)" in adopt
    assert "nativeTasks=rows.filter" in adopt
    assert "pcWM.place" not in adopt


def test_maximise_is_geometry_only_and_never_recreates_the_app():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    block = src[src.index("function snapTo"):src.index("// The Snap Layouts flyout")]
    for destructive in ("renderView(", "openApp(", "closeWin(", "innerHTML"):
        assert destructive not in block, f"maximise/restore recreates app state through {destructive}"
    assert "Object.assign(w.el.style" in block


def test_native_apps_inherit_the_dark_gtk_chrome():
    for path in (
        ROOT / "os/bin/pc-shell-start",
        ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-shell-start",
    ):
        start = path.read_text(encoding="utf-8")
        assert 'GTK_THEME="${GTK_THEME:-Adwaita:dark}"' in start
        assert "GTK_APPLICATION_PREFER_DARK_THEME=1" in start


def test_resized_and_rejected_handoff_windows_stay_inside_the_desktop():
    src = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    assert "function keepFrameReachable(w)" in src
    resize = src[src.index("function startResize"):src.index("// ---- desktop, taskbar")]
    assert "vwL()-left-12" in resize
    assert "vhL()-TASKBAR-top-12" in resize
    handoff = src[src.index("if(handoff && w.native == null"):src.index("_natGesture(w, false)",
                                                                          src.index("if(handoff && w.native == null"))]
    assert "keepFrameReachable(w)" in handoff
