from pathlib import Path


OS = (Path(__file__).resolve().parents[1] / "static/js/client/os.js").read_text(encoding="utf-8")


def _body(start, end):
    return OS[OS.index(start):OS.index(end, OS.index(start))]


def test_real_posterchanos_can_enter_even_during_a_narrow_surface_measurement():
    enter = _body("function enter(){", "function exit(")
    assert "if(!fits() && !isSystemShell())" in enter


def test_real_posterchanos_resize_can_never_fall_through_to_classic():
    resize = _body("function onResize(){", "function onKey")
    # Resize preserves the desktop for every already-entered surface. Browsers/tablets use the
    # portrait layout below; the real system shell also remains entered. Only enter() owns the
    # initial width refusal, so a transient compositor measurement can never call exit().
    assert "const portrait=!fits() && !isSystemShell()" in resize
    assert "exit();" not in resize


def test_browser_and_tablet_desktop_still_keep_the_width_gate():
    helper = _body("function isSystemShell(){", "function enter(){")
    assert "PCOSShell.available()" in helper
    assert "return false" in helper
    restore = _body("function restore(){", "let _superClean")
    assert "settings().get(KEY, false) && fits()" in restore
