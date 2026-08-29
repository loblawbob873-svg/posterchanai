"""Tablet focus and rotation preserve managed desktop windows."""

from pathlib import Path
import json
import subprocess


OS = (Path(__file__).parents[2] / "static/js/client/os.js").read_text()


def test_keyboard_height_change_is_not_treated_as_monitor_resize():
    body = OS[OS.index("function onResize()"):
              OS.index("function onKey", OS.index("function onResize()"))]
    assert "const editing=" in body
    assert "_keyboardHeightChange(_deskLayoutSize,logical,editing,_keyboardViewport" in body
    assert "if(!heightOnly) _keyboardViewport=false" in body
    assert "pointer:coarse" not in body


def test_editor_blur_before_keyboard_resize_is_still_classified_as_keyboard_change():
    """Execute the shipped decision function for the focusout-before-resize ordering."""
    start = OS.index("function _keyboardHeightChange(")
    brace = OS.index("{", start)
    depth = 0
    end = None
    for pos in range(brace, len(OS)):
        if OS[pos] == "{":
            depth += 1
        elif OS[pos] == "}":
            depth -= 1
            if depth == 0:
                end = pos + 1
                break
    assert end
    fn = OS[start:end]
    script = fn + "\nconsole.log(JSON.stringify([" \
        "_keyboardHeightChange({w:1200,h:800},{w:1200,h:500},false,false,true)," \
        "_keyboardHeightChange({w:1200,h:800},{w:1200,h:500},false,false,false)," \
        "_keyboardHeightChange({w:1200,h:800},{w:900,h:500},false,false,true)]));"
    got = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert got == [True, False, False]


def test_edit_blur_grace_is_installed_and_removed_with_desktop():
    assert "document.addEventListener('focusout', _noteDesktopEditBlur, true)" in OS
    assert "document.removeEventListener('focusout', _noteDesktopEditBlur, true)" in OS


def test_active_desktop_survives_portrait_and_restores_snap_sides():
    body = OS[OS.index("function onResize()"):
              OS.index("function onKey", OS.index("function onResize()"))]
    assert "exit();" not in body
    assert "w.rotationSnap=w.rotationSnap||w.snap" in body
    assert "w.snap=w.rotationSnap" in body
    assert "if(portrait && w.snap && w.snap!=='max')" in body
