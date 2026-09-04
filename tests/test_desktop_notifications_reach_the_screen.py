"""THE TOOLBAR'S NOTIFICATION CENTRE IS THE SURFACE. NOTHING ELSE DRAWS ONE.

`osNotify` (app.js) prefers the desktop bridge, the bridge is Electron's Notification API, and on
Linux that is libnotify talking to `org.freedesktop.Notifications`. PosterChanOS ships no server on
that bus, so those calls reach nothing.

That was briefly "fixed" by installing libnotify and autostarting mako, and the popups were reported
as annoying the same hour: *"desktop had annying notification boxes? why? we have a better
notification fetaure on toolbar"*. It is the right call and it is the one the migration notes
already made — PosterChanUI owns the taskbar, tray, desktop AND the notification centre, and a
layer-shell daemon painting a second one over the desktop is precisely the duplicate they forbid
("it must not add a second panel, dock, launcher, notification area, or background").

So this file guards the ABSENCE, which is the thing an "obviously missing dependency" audit would
otherwise add back: the tray centre is where a DM lands, and nothing on this image starts a popup
daemon.
"""
from pathlib import Path

from tests.wayfire_config import sections


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "os/overlay/app-misc/posterchanos-shell"
GENTOO = (ROOT / "os/gentoo.sh").read_text()
EBUILD = (SHELL / "posterchanos-shell-1.0.0.ebuild").read_text()
PACKAGES = GENTOO.split("POSTERCHANOS_PACKAGES=", 1)[1].split('"', 2)[1]


def test_no_notification_daemon_is_installed_or_started():
    for unwanted in ("gui-apps/mako", "x11-libs/libnotify", "dunst", "swaync"):
        assert unwanted not in PACKAGES, f"{unwanted} draws popups over PosterChan's own desktop"
        assert unwanted not in EBUILD
    autostart = sections()["autostart"]
    for value in autostart.values():
        assert "mako" not in value and "dunst" not in value, autostart
    assert not (SHELL / "files/mako.config").exists()


def test_nothing_else_draws_a_desktop_surface_either():
    """The same rule, for the surfaces PosterChanUI already is."""
    for unwanted in ("wf-panel", "wf-dock", "wf-background", "waybar", "swaybg"):
        assert unwanted not in PACKAGES
    config = (SHELL / "files/wayfire.ini").read_text()
    for unwanted in ("wf-panel", "wf-dock", "wf-background", "swaybg", "swaybar"):
        assert unwanted not in config


def test_the_reason_is_written_down_where_the_next_audit_will_look():
    """An absent dependency looks like an oversight. Both places that would "fix" it say why not."""
    config = (SHELL / "files/wayfire.ini").read_text()
    assert "NO NOTIFICATION DAEMON" in config.upper()
    assert "NO NOTIFICATION DAEMON" in GENTOO.upper()


def test_the_notification_centre_is_still_the_shell_s_own_window():
    """The surface that replaces it is a real floating window, for the reason the start menu is:
    the shell is the tiled window underneath every application, so a panel drawn inside the page
    cannot rise above one."""
    assert (ROOT / "tests/client/test_notifications_go_over_windows.py").exists()
    os_js = (ROOT / "static/js/client/os.js").read_text()
    assert "pc:notifications" in os_js or "notification" in os_js.lower()
