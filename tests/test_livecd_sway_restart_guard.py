"""The LiveCD must fail visibly instead of flashing black forever when Sway cannot start."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "os" / "gentoo.sh").read_text(encoding="utf-8")
LAUNCHERS = [
    ROOT / "os" / "bin" / "pc-shell-start",
    ROOT / "os" / "overlay" / "app-misc" / "posterchanos-shell" / "files" / "pc-shell-start",
]


def test_every_generated_login_profile_has_a_boot_scoped_restart_guard():
    # gentoo.sh writes three profiles: final installed target, upgrade/session setup, and LiveCD.
    assert INSTALLER.count('pc_guard="$HOME/.local/state/posterchanos/sway-boot-attempt"') == 3
    assert INSTALLER.count('pc_boot_id=$(cat /proc/sys/kernel/random/boot_id') == 3
    assert INSTALLER.count('if [ "$pc_attempts" -gt 2 ]; then') == 3
    assert INSTALLER.count("stopped a graphical-session restart loop") == 3


def test_successful_shell_mapping_rearms_recovery_for_later_restarts():
    for launcher in LAUNCHERS:
        source = launcher.read_text(encoding="utf-8")
        mapped = source.index('echo "PosterChan shell window mapped"')
        cleared = source.index('rm -f "$USER_HOME/.local/state/posterchanos/sway-boot-attempt"')
        exited = source.index("exit 0", mapped)
        assert mapped < cleared < exited, launcher


def test_packaged_and_installer_launchers_stay_identical():
    assert LAUNCHERS[0].read_bytes() == LAUNCHERS[1].read_bytes()


def test_live_profile_and_launcher_establish_home_before_first_use():
    profile = INSTALLER.split("cat >\"$WORK/live.bash_profile\" <<'PROFILE'", 1)[1].split(
        "\nPROFILE", 1
    )[0]
    assert profile.index('HOME=$(getent passwd "$(id -un)" | cut -d: -f6)') < profile.index(
        "[[ -f ~/.bashrc ]]"
    )
    for launcher in LAUNCHERS:
        source = launcher.read_text(encoding="utf-8")
        home = source.index('HOME=$(getent passwd "$(id -un)" | cut -d: -f6)')
        first_use = source.index('"/run/user/$(id -u)/posterchan-shell-start.lock"')
        assert home < first_use


def test_shell_binds_gtk_to_native_wayland_before_electron_launch():
    for launcher in LAUNCHERS:
        source = launcher.read_text(encoding="utf-8")
        backend = source.index('export GDK_BACKEND="${GDK_BACKEND:-wayland}"')
        launch = source.index('"$PC_DESKTOP_LAUNCHER" --shell --ozone-platform=wayland')
        assert backend < launch


def test_reloadable_sway_configs_do_not_force_xwayland_at_startup():
    """Sway accepts this startup directive but reports it as an error on every live reload."""
    packaged = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text(
        encoding="utf-8"
    )
    installer = (ROOT / "os/gentoo.sh").read_text(encoding="utf-8")
    for source in (packaged, installer):
        assert "xwayland force" not in source
