"""A DM NOTIFICATION HAD NOWHERE TO GO, AND NOTHING SAID SO.

Measured on the running desktop, in its own log:

    WARNING:electron/shell/browser/notifications/linux/libnotify_notification.cc:87]
    Unable to find libnotify; notifications disabled

`osNotify` (app.js) prefers the desktop bridge over every other path, the bridge is Electron's
Notification API, and on Linux that is libnotify talking to `org.freedesktop.Notifications`.
PosterChanOS shipped neither the library nor a server, so `Notification.isSupported()` answered
false, `pc:host:notify` returned false, and every DM, mention and call notification this desktop has
ever raised was dropped -- while Settings reported "granted", because `osNotifyState` only ever
checked that the bridge EXISTS.

The popup is a DAEMON rather than something the shell draws, for the reason the start menu had to
stop being a div: a transient notification must appear over whatever is on screen, including a
fullscreen game, and the shell surface is the tiled window underneath every one of those. mako is a
layer-shell client, so the compositor stacks it. It is not a second panel/dock/launcher -- it draws
nothing at all until something notifies.
"""
from pathlib import Path
import re

from tests.wayfire_config import sections


ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "os/overlay/app-misc/posterchanos-shell"
GENTOO = (ROOT / "os/gentoo.sh").read_text()
EBUILD = (SHELL / "posterchanos-shell-1.0.0.ebuild").read_text()


def test_the_desktop_really_does_call_the_native_notification_api():
    """The premise. If this stops being true the rest of the file is guarding nothing."""
    main = (ROOT / "desktop/main.js").read_text()
    handler = main.split("ipcMain.handle('pc:host:notify'", 1)[1].split("ipcMain.handle(", 1)[0]
    assert "electron.Notification" in handler
    assert "isSupported()" in handler
    app = (ROOT / "static/js/client/app.js").read_text()
    body = app.split("function osNotify(", 1)[1].split("\n  }\n", 1)[0]
    assert "pcHost.notify" in body


def test_both_halves_of_the_notification_path_are_installed():
    """The library AND a server. Either one alone still shows nothing."""
    packages = GENTOO.split("POSTERCHANOS_PACKAGES=", 1)[1].split('"', 2)[1]
    assert "x11-libs/libnotify" in packages, "Electron cannot raise a notification at all"
    assert "gui-apps/mako" in packages, "nothing owns org.freedesktop.Notifications on this session"
    # The shell package depends on them too: an installed machine gains them through an update,
    # not only a fresh build.
    assert "gui-apps/mako" in EBUILD
    assert "x11-libs/libnotify" in EBUILD


def test_the_server_starts_with_the_session():
    autostart = sections()["autostart"]
    assert any(v.strip().split()[0].endswith("mako") for v in autostart.values()), autostart


def test_the_popup_is_stacked_above_applications_and_styled_like_this_desktop():
    """`layer=overlay` is the whole reason a daemon was chosen over drawing it in the shell."""
    cfg = (SHELL / "files/mako.config").read_text()
    # Only the GLOBAL section: mako's `[urgency=critical]` block below re-states some of these, and
    # reading the file as one flat map silently takes the override as the default.
    values = dict(re.findall(r"^([a-z-]+)=(.+)$", cfg.split("\n[", 1)[0], re.M))
    assert values.get("layer") == "overlay", "a notification can be covered by a fullscreen game"
    assert values.get("anchor") == "top-right"
    # A notification the user never dismisses must not stay on screen for ever, and a CALL must not
    # time out before it is answered -- the two cases mako's urgency section separates.
    assert int(values["default-timeout"]) > 0
    assert "[urgency=critical]" in cfg
    assert "default-timeout=0" in cfg.split("[urgency=critical]", 1)[1]
    assert 'newins "${FILESDIR}/mako.config" config' in EBUILD
    assert "insinto /etc/xdg/mako" in EBUILD


def test_nothing_else_draws_a_desktop_surface():
    """mako answers notifications and nothing else. A panel, dock, launcher or wallpaper daemon
    would be a second copy of something PosterChanUI already is."""
    cfg = (SHELL / "files/mako.config").read_text()
    for unwanted in ("wf-panel", "wf-dock", "wf-background", "waybar", "swaybg"):
        assert unwanted not in GENTOO.split("POSTERCHANOS_PACKAGES=", 1)[1].split('"', 2)[1]
        assert unwanted not in cfg
