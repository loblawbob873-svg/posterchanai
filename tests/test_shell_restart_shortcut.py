from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ctrl_alt_backspace_restarts_only_the_posterchan_shell():
    cfg = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text()
    line = next(x for x in cfg.splitlines() if "Ctrl+Mod1+22" in x)
    assert "bindcode --no-repeat" in line
    assert "/usr/local/bin/pc-shell-restart" in line
    assert "swaymsg exit" not in line
    assert "systemctl" not in line


def test_installer_ships_the_same_recovery_binding():
    installer = (ROOT / "os/gentoo.sh").read_text()
    assert "bindcode --no-repeat Ctrl+Mod1+22" in installer
    assert "/usr/local/bin/pc-shell-restart" in installer


def test_shell_package_installs_the_config_name_sway_actually_reads():
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert 'newins "${FILESDIR}/sway.config" config' in ebuild
    assert 'doins "${FILESDIR}/sway.config"' not in ebuild


def test_shell_package_migrates_existing_identity_configs_without_replacing_them():
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert '"${EROOT%/}"/home/pc-*/.config/sway/config' in ebuild
    assert "grep -q 'Ctrl+Mod1+22'" in ebuild
    assert 'cat >>"${cfg}"' in ebuild


def test_shell_restart_is_serialized_and_targets_only_the_shell_process():
    start = (ROOT / "os/bin/pc-shell-start").read_text()
    restart = (ROOT / "os/bin/pc-shell-restart").read_text()
    assert "flock -n 9" in start
    assert "posterchan-shell-start.lock" in start
    assert "posterchan-desktop --shell" in restart
    assert "pkill -TERM" in restart and "pkill -KILL" in restart
