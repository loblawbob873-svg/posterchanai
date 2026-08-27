from pathlib import Path


OS = (Path(__file__).resolve().parents[1] / "static/js/client/os.js").read_text(encoding="utf-8")


def _body(start, end):
    return OS[OS.index(start):OS.index(end, OS.index(start))]


def test_real_posterchanos_can_enter_even_during_a_narrow_surface_measurement():
    enter = _body("function enter(){", "function exit(")
    assert "if(!fits() && !isSystemShell())" in enter


def test_real_posterchanos_resize_can_never_fall_through_to_classic():
    resize = _body("function onResize(){", "function onKey")
    assert "if(!fits() && !isSystemShell())" in resize
    guarded = resize.split("if(!fits() && !isSystemShell())", 1)[1]
    assert "exit();" in guarded


def test_browser_and_tablet_desktop_still_keep_the_width_gate():
    helper = _body("function isSystemShell(){", "function enter(){")
    assert "PCOSShell.available()" in helper
    assert "return false" in helper
    restore = _body("function restore(){", "let _superClean")
    assert "settings().get(KEY, false) && fits()" in restore
