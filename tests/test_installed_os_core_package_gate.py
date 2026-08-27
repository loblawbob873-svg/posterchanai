from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / "scripts/check_installed_os_core_package.sh"


def test_gate_parses_and_is_explicitly_installed_package_scoped():
    result = subprocess.run(["bash", "-n", str(GATE)], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    source = GATE.read_text()
    assert "qlist -Iv app-misc/posterchan-desktop" in source
    assert "qlist -Iv app-misc/posterchanos-shell" in source
    assert 'qfile -q "$file"' in source


def test_gate_covers_update_first_run_and_security_critical_modes():
    source = GATE.read_text()
    for path in ("/opt/posterchan/posterchan-desktop",
                 "/opt/posterchan/resources/tor/tor/tor",
                 "/usr/local/bin/update-posterchan",
                 "/usr/local/bin/pc-shell-start", "/usr/bin/gentoo.sh"):
        assert path in source
    assert "chrome-sandbox" in source and "4755" in source
    assert source.count("sudoers.d/") >= 2 and "440" in source
    assert "posterchan-update.lock" in source
    assert "emaint sync -r posterchan" in source
    assert "exec sway" in source and "autologin_user" in source
    assert 'getent passwd "$autologin_user"' in source
    # PosterChanOS installs Gentoo's prebuilt www-client/firefox-bin package.  It intentionally
    # exposes /usr/bin/firefox-bin rather than relying on a distribution-specific `firefox` alias.
    assert "firefox-bin" in source
