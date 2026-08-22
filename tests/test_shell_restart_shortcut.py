from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_ctrl_alt_backspace_restarts_only_the_posterchan_shell():
    cfg = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text()
    line = next(x for x in cfg.splitlines() if "Ctrl+Mod1+BackSpace" in x)
    assert "bindsym --no-repeat" in line
    assert "/usr/local/bin/pc-shell-restart" in line
    assert "swaymsg exit" not in line
    assert "systemctl" not in line


def test_installer_ships_the_same_recovery_binding():
    installer = (ROOT / "os/gentoo.sh").read_text()
    assert "bindsym --no-repeat Ctrl+Mod1+BackSpace" in installer
    assert "/usr/local/bin/pc-shell-restart" in installer


def test_shell_package_installs_the_config_name_sway_actually_reads():
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert 'newins "${FILESDIR}/sway.config" config' in ebuild
    assert 'doins "${FILESDIR}/sway.config"' not in ebuild


def test_shell_package_migrates_existing_identity_configs_without_replacing_them():
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert '"${EROOT%/}"/home/pc-*/.config/sway/config' in ebuild
    assert "Ctrl\\+Mod1\\+(BackSpace|22)" in ebuild
    assert "Super_L exec swaymsg -t send_tick pc:start" in ebuild
    assert 'cat >>"${cfg}"' in ebuild


def test_shell_restart_is_serialized_and_targets_only_the_shell_process():
    start = (ROOT / "os/bin/pc-shell-start").read_text()
    restart = (ROOT / "os/bin/pc-shell-restart").read_text()
    main = (ROOT / "desktop/main.js").read_text()
    assert "flock -n 9" in start
    assert "posterchan-shell-start.lock" in start
    assert "pattern='[/]opt/posterchan/'" in restart
    assert "send_tick pc:restart" in restart
    assert "pkill" not in restart
    assert "reloadIgnoringCache" in main
    assert "ev.payload !== 'pc:restart'" in main
    assert "exec /usr/local/bin/pc-shell-start" in restart
    assert "retries" in start and "exit 1" in start


def test_upgrade_removes_optioned_printscreen_bindings_before_adding_one_copy():
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    assert "bindsym .*?(Print|Ctrl\\+Shift\\+s|Shift\\+Print)" in ebuild
    assert "outputs.conf" in ebuild
    assert "include ~/.config/sway/outputs.conf" in ebuild
    assert "floating_modifier $mod normal" in ebuild


def test_super_is_a_global_physical_key_binding_not_a_bare_modifier_binding():
    cfg = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text()
    assert "bindsym --release --no-repeat Super_L exec swaymsg -t send_tick pc:start" in cfg
    assert "bindsym --release --no-repeat $mod exec swaymsg -t send_tick pc:start" not in cfg


def test_alt_tab_is_compositor_owned_and_migrated_to_existing_accounts():
    cfg = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text()
    ebuild = (ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild").read_text()
    helper = ROOT / "os/bin/pc-window-cycle"
    assert "Mod1+Tab exec /usr/local/bin/pc-window-cycle next" in cfg
    assert "Mod1+Shift+Tab exec /usr/local/bin/pc-window-cycle previous" in cfg
    assert "pc-window-cycle" in ebuild
    assert helper.exists()
