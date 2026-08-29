import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
import checkall


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


def test_installed_diagnostic_cannot_write_the_real_sway_outputs_file():
    """The nested HEADLESS output must never escape into the signed-in desktop's config."""
    main = (Path(__file__).parents[1] / "desktop" / "main.js").read_text(encoding="utf-8")
    start = main.index("function displays(){")
    block = main[start:main.index("ipcMain.handle('pc:wm:available'", start)]
    assert "diagnostic.profile" in block
    assert "sway-outputs.conf" in block
    assert "new Displays(wm(), opts)" in block


def test_installed_settings_gate_is_a_serial_release_check():
    job = {row["name"]: row for row in checkall.discover()}["check_installed_system_settings"]
    assert job["registered"] is True
    assert job["serial"] is True
    assert job["env"] == {"PC_CHECK_PORT": "9223"}


def test_installed_settings_gate_skips_only_when_cdp_is_unattached():
    env = {**os.environ, "PC_CHECK_PORT": "1"}
    result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / "scripts" /
                            "check_installed_system_settings.py")], env=env,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "SKIP installed Electron is not attached on loopback CDP" in result.stdout
