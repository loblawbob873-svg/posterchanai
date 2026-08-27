"""Tablet focus and rotation preserve managed desktop windows."""

from pathlib import Path


OS = (Path(__file__).parents[2] / "static/js/client/os.js").read_text()


def test_keyboard_height_change_is_not_treated_as_monitor_resize():
    body = OS[OS.index("function onResize()"):
              OS.index("function onKey", OS.index("function onResize()"))]
    assert "const editing=" in body
    assert "heightOnly&&(editing||_keyboardViewport)" in body
    assert "if(!heightOnly) _keyboardViewport=false" in body
    assert "pointer:coarse" not in body


def test_active_desktop_survives_portrait_and_restores_snap_sides():
    body = OS[OS.index("function onResize()"):
              OS.index("function onKey", OS.index("function onResize()"))]
    assert "exit();" not in body
    assert "w.rotationSnap=w.rotationSnap||w.snap" in body
    assert "w.snap=w.rotationSnap" in body
    assert "if(portrait && w.snap && w.snap!=='max')" in body
