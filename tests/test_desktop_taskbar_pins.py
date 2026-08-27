"""Persistent PosterChanOS taskbar pins."""
from pathlib import Path


SRC = (Path(__file__).parents[1] / "static/js/client/os.js").read_text()


def test_layout_document_keeps_bounded_namespaced_pins():
    assert "pins: []" in SRC
    assert "/^(view|app):" in SRC
    assert "out.pins.length >= 24" in SRC


def test_closed_pins_are_drawn_and_open_windows_are_not_duplicated():
    assert "const openViews = new Set" in SRC
    assert "const openApps = new Set" in SRC
    assert 'data-kind="pin-view"' in SRC
    assert 'data-kind="pin-app"' in SRC


def test_start_menu_and_taskbar_offer_pin_and_unpin():
    assert SRC.count("Pin to taskbar") >= 2
    assert SRC.count("Unpin from taskbar") >= 2
    assert "setPinned('app', app, !pinned)" in SRC
    assert "setPinned('view', view, !pinned)" in SRC


def test_running_task_context_menu_can_move_recover_and_close_windows():
    assert "function taskbarMove(w)" in SRC
    assert "osw-taskbar-moving" in SRC
    assert "{label:'Move',run:()=>taskbarMove(running)}" in SRC
    assert "{label:'Close',run:()=>closeWin(running)}" in SRC
    assert "keepFrameReachable(w);_natGesture(w,false)" in SRC
    assert "function nativeTaskbarMove(row)" in SRC
    assert "{label:'Move',run:()=>nativeTaskbarMove(w)}" in SRC
    assert "if(!w)w=adoptNative(row)" in SRC
    menu = SRC[SRC.index("$$('.os-task', bar).forEach(b => b.oncontextmenu"):
               SRC.index("$$('.os-native-max'", SRC.index("$$('.os-task', bar).forEach(b => b.oncontextmenu"))]
    assert "move position cursor" not in menu


def test_adopted_native_task_gets_move_and_close_without_an_ephemeral_pin():
    menu = SRC[SRC.index("$$('.os-task', bar).forEach(b => b.oncontextmenu"):
               SRC.index("$$('.os-native-max'", SRC.index("$$('.os-task', bar).forEach(b => b.oncontextmenu"))]
    assert "running.native==null" in menu
    assert "if(running)actions.push({label:'Move'" in menu
    assert "{label:'Close',run:()=>closeWin(running)}" in menu
    assert "if(key){" in menu


def test_taskbar_context_menu_is_anchored_in_the_desktops_scaled_coordinate_space():
    start = SRC.index("function showCtx(")
    body = SRC[start:SRC.index("function iconMenu(", start)]
    assert "desk.getBoundingClientRect()" in body
    assert "desk.offsetWidth" in body and "desk.offsetHeight" in body
    assert "ar.left-dr.left" in body and "ar.top-dr.top" in body
    # Both native and PosterChan task buttons pass the button, not just viewport pointer coordinates.
    task = SRC[SRC.index("$$('.os-task', bar).forEach(b => b.oncontextmenu"):
               SRC.index("$$('.os-native-max'", SRC.index("$$('.os-task', bar).forEach(b => b.oncontextmenu"))]
    assert task.count("],b);") >= 1
    assert "showCtx(e.clientX, e.clientY, actions, b)" in task


def test_start_menu_can_add_and_remove_apps_from_desktop():
    assert "Add ' + label + ' to the desktop" in SRC
    assert "Hide ' + label + ' from the desktop" in SRC
    assert "showItem(view)" in SRC
    assert "hideItem(view)" in SRC
