from pathlib import Path


SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" / "check_installed_native_snap.py").read_text()


def test_installed_native_snap_uses_a_real_drag_and_requires_preview():
    assert ".osw-native[data-pc-check-native" in SCRIPT
    assert "new PointerEvent('pointerdown'" in SCRIPT
    assert "new PointerEvent('pointermove'" in SCRIPT
    assert "new PointerEvent('pointerup'" in SCRIPT
    assert ".os-ghost" in SCRIPT
    assert "assert preview" in SCRIPT


def test_installed_native_snap_compares_frame_surface_and_output():
    assert "frame.classList.contains('snapped')" in SCRIPT
    assert 'not snapped["native"]["stashed"]' in SCRIPT
    assert 'abs(native["width"] - snapped["frame"]["w"]) < 40' in SCRIPT
    assert 'shell["width"] * .4 < native["width"] < shell["width"] * .6' in SCRIPT


def test_installed_native_snap_restores_geometry_and_privacy():
    assert "pcWM.restore" in SCRIPT
    assert "initially_stashed" in SCRIPT
    assert "pcWM.hide" in SCRIPT and "pcWM.focus" in SCRIPT
    assert "textContent" not in SCRIPT and "outerHTML" not in SCRIPT
