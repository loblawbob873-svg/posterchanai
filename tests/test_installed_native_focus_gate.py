from pathlib import Path


SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" / "check_installed_native_focus.py").read_text()


def test_installed_native_focus_gate_uses_real_dom_and_compositor_state():
    assert "PCOS.refresh()" in SCRIPT
    assert "await pcWM.snapshot()" in SCRIPT
    assert ".osw-native[data-pc-check-native" in SCRIPT
    assert ".osw:not(.osw-native) .osw-bar" in SCRIPT
    assert 'covered["nativeStashed"] and covered["compositorStashed"]' in SCRIPT
    assert 'covered["shellFocused"]' in SCRIPT
    assert 'overlapped["nativeStashed"] and overlapped["compositorStashed"]' in SCRIPT
    assert '"blackManagedOverlap"' in SCRIPT
    assert '"managedOverlapDidNotRestore"' in SCRIPT


def test_installed_native_focus_gate_is_private_and_restores_state():
    assert "window pixels" in SCRIPT
    assert "initiallyStashed" in SCRIPT
    assert "await pcWM.hide(info.id)" in SCRIPT
    assert "await pcWM.focus(info.id)" in SCRIPT
    assert "finally:" in SCRIPT
    assert "w.title" not in SCRIPT
    assert "textContent" not in SCRIPT
    assert "outerHTML" not in SCRIPT
