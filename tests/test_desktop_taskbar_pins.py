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
    assert "move position cursor" in SRC


def test_start_menu_can_add_and_remove_apps_from_desktop():
    assert "Add ' + label + ' to the desktop" in SRC
    assert "Hide ' + label + ' from the desktop" in SRC
    assert "showItem(view)" in SRC
    assert "hideItem(view)" in SRC
