"""The LiveCD must fail visibly instead of flashing black forever when Sway cannot start."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "os" / "gentoo.sh").read_text(encoding="utf-8")
LAUNCHERS = [
    ROOT / "os" / "bin" / "pc-shell-start-wayfire",
    ROOT / "os" / "overlay" / "app-misc" / "posterchanos-shell" / "files" / "pc-shell-start-wayfire",
]


def test_every_generated_login_profile_has_a_boot_scoped_restart_guard():
    # gentoo.sh writes three profiles: final installed target, upgrade/session setup, and LiveCD.
    assert INSTALLER.count('pc_guard="$HOME/.local/state/posterchanos/compositor-boot-attempt"') == 3
    assert INSTALLER.count('pc_boot_id=$(cat /proc/sys/kernel/random/boot_id') == 3
    assert INSTALLER.count('if [ "$pc_attempts" -gt 2 ]; then') == 3
    assert INSTALLER.count("stopped a graphical-session restart loop") == 3


def test_successful_shell_mapping_rearms_recovery_for_later_restarts():
    for launcher in LAUNCHERS:
        source = launcher.read_text(encoding="utf-8")
        mapped = source.index('echo "PosterChan shell window mapped"')
        cleared = source.index('.local/state/posterchanos/compositor-boot-attempt"')
        # It has to be cleared on the VERIFIED path, before the launcher settles into waiting on the
        # shell -- clearing it on the way out would re-arm the guard for a shell that never mapped.
        waited = source.index('wait "$shell_pid"; exit $?', mapped)
        assert mapped < cleared < waited, launcher


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
        home = source.index('getent passwd "$(id -un)" | cut -d: -f6')
        first_use = source.index("posterchan-shell-start.lock")
        assert home < first_use, launcher


def test_live_profile_uses_tty1_when_pam_omits_xdg_vtnr():
    profile = INSTALLER.split("cat >\"$WORK/live.bash_profile\" <<'PROFILE'", 1)[1].split(
        "\nPROFILE", 1
    )[0]
    assert '[ "${XDG_VTNR:-}" = 1 ] || [ "$(tty)" = /dev/tty1 ]' in profile


def test_shell_binds_gtk_to_native_wayland_before_electron_launch():
    for launcher in LAUNCHERS:
        source = launcher.read_text(encoding="utf-8")
        backend = source.index('export GDK_BACKEND="${GDK_BACKEND:-wayland}"')
        launch = source.index('"$launcher" --shell --ozone-platform=wayland')
        assert backend < launch


def test_the_session_config_does_not_force_xwayland_at_startup():
    """Sway accepted `xwayland force` at startup and then reported it as an error on every live
    reload. Wayfire states it declaratively (`xwayland = true` in [core]) and has no reload
    directive to get wrong -- so what is checked is that the startup-only spelling has not come
    back into either source."""
    packaged = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini").read_text(
        encoding="utf-8"
    )
    installer = (ROOT / "os/gentoo.sh").read_text(encoding="utf-8")
    assert "xwayland = true" in packaged
    for source in (packaged, installer):
        assert "xwayland force" not in source
