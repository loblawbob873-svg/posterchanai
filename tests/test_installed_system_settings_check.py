from pathlib import Path


SRC = (Path(__file__).parents[1] / "scripts" / "check_installed_system_settings.py").read_text()


def test_installed_settings_gate_is_account_independent_and_drives_real_ui():
    assert "choose_authenticated_page" not in SRC
    assert 'startswith(\"app://posterchan/\")' in SRC
    assert "PCOS.openSystemSettings()" in SRC
    assert "button.click()" in SRC
    assert "pane&&!pane.hidden" in SRC


def test_installed_settings_gate_checks_separation_mobile_access_and_focus_return():
    for page in ("displays", "appearance", "sound", "network", "bluetooth", "power",
                 "users", "updates", "about", "liveusb"):
        assert f"'{page}'" in SRC
    assert '"page:" + page' in SRC
    assert "widgetControls" in SRC
    assert "PCOS.routeView('code')" in SRC
    assert "returned" in SRC and "isolated" in SRC


def test_installed_settings_gate_closes_only_its_created_windows():
    assert "__pcInstalledSettingsBackup={before,focused:" in SRC
    assert ".filter(w=>!backup.before.has(w))" in SRC
    assert "const close=w.querySelector('.osw-x');if(close)close.click()" in SRC
