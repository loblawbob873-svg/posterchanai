from pathlib import Path


SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" / "check_installed_native_handoff.py").read_text()


def test_installed_handoff_drives_the_title_bar_not_the_bridge_shortcut():
    assert ".osw-native[data-pc-check-native" in SCRIPT
    assert "new PointerEvent('pointerdown'" in SCRIPT
    assert "new PointerEvent('pointermove'" in SCRIPT
    assert "new PointerEvent('pointerup'" in SCRIPT
    assert ".handoff(" not in SCRIPT


def test_installed_handoff_requires_one_frame_and_no_spurious_html_app():
    assert "assert len(owners) == 1" in SCRIPT
    assert '== html_counts' in SCRIPT
    assert 'not moved[destination]["frameStashed"]' in SCRIPT
    assert 'not moved[destination]["native"]["stashed"]' in SCRIPT
    assert "assert_paired(moved[destination])" in SCRIPT
    assert 'row["chrome"] and row["border"]' in SCRIPT
    assert 'row["frameFocused"] and row["native"]["focused"]' in SCRIPT
    assert 'abs(mapped[k] - native[k if k in ("x", "y")' in SCRIPT


def test_installed_handoff_returns_the_same_native_id_and_privacy_state():
    assert 'restored[source]["native"]["id"] == native_id' in SCRIPT
    assert "initially_stashed" in SCRIPT
    assert "pcWM.hide" in SCRIPT and "pcWM.focus" in SCRIPT
    assert "textContent" not in SCRIPT
    assert "outerHTML" not in SCRIPT
