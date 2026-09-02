"""System Settings categories do not wait on unrelated hardware bridges."""

from pathlib import Path


OS = (Path(__file__).parents[2] / "static/js/client/os.js").read_text(encoding="utf-8")


def _renderer():
    start = OS.index("async function renderSystemSettings()")
    return OS[start:OS.index("\n  function openTaskManager", start)]


def test_hardware_reads_are_scoped_to_their_real_settings_pages():
    body = _renderer()
    display_guard = body.index("if(_osSettingsPage==='displays')")
    display_read = body.index("pcDisplays.status()")
    power_guard = body.index("if(_osSettingsPage==='power')")
    power_read = body.index("pcPower.status()")
    about_guard = body.index("if(_osSettingsPage==='about')")
    about_read = body.index("pcSystem.snapshot(false)")
    assert display_guard < display_read < power_guard < power_read < about_guard < about_read


def test_desktop_and_mobile_category_changes_load_the_new_section_owner():
    body = _renderer()
    assert "_osSettingsPage=b.dataset.page;renderSystemSettings();" in body
    assert "_osSettingsPage=value;renderSystemSettings();" in body
    assert "_osSettingsPage=b.dataset.page;draw();" not in body


def test_liveusb_remains_one_coherent_settings_page():
    body = _renderer()
    assert body.count('data-settings-page="liveusb"') == 1
    section = body[body.index('data-liveusb data-settings-page="liveusb"'):]
    assert "data-live-build" in section and "data-live-burn" in section
