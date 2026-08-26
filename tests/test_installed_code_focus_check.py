from pathlib import Path


SRC = (Path(__file__).parents[1] / "scripts" / "check_installed_code_focus.py").read_text()


def test_focus_gate_checks_both_layout_classes_in_both_focus_states():
    assert SRC.count("includes('feed-code')") == 4
    assert SRC.count("includes('feed-term')") == 4
    assert "Code and Terminal did not open as distinct windows" in SRC


def test_focus_gate_drives_real_desktop_windows():
    assert "PCOS.routeView('code')" in SRC
    assert "PCOS.routeView('terminal')" in SRC
    assert "new PointerEvent('pointerdown'" in SRC
