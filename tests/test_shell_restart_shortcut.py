from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ctrl_alt_backspace_restarts_only_the_posterchan_shell():
    cfg = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text()
    line = next(x for x in cfg.splitlines() if "Ctrl+Mod1+BackSpace" in x)
    assert "--release --no-repeat" in line
    assert "pkill -f posterchan[-]desktop" in line
    assert "/usr/local/bin/pc-shell-start" in line
    assert "swaymsg exit" not in line
    assert "systemctl" not in line


def test_installer_ships_the_same_recovery_binding():
    installer = (ROOT / "os/gentoo.sh").read_text()
    assert "Ctrl+Mod1+BackSpace" in installer
    assert "pkill -f posterchan[-]desktop" in installer


def test_shell_package_installs_the_config_name_sway_actually_reads():
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert 'newins "${FILESDIR}/sway.config" config' in ebuild
    assert 'doins "${FILESDIR}/sway.config"' not in ebuild


def test_shell_package_migrates_existing_identity_configs_without_replacing_them():
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert '"${EROOT%/}"/home/pc-*/.config/sway/config' in ebuild
    assert "grep -q 'Ctrl+Mod1+BackSpace'" in ebuild
    assert 'cat >>"${cfg}"' in ebuild
