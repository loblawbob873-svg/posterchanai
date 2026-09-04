"""The desktop fills the screen without claiming it, or it hides every app it hosts.

    "clicking on a screenshot file loads black screen with spinning circle"

Measured on the machine: opening a screenshot gave mupdf a window at 35,20 1849x1040 with
`visible: false`, behind a shell reporting `fullscreen_mode: 1`. The app was running, placed and
correctly sized — and behind us, so its frame drew over nothing. A compositor fullscreen window
covers its entire workspace INCLUDING every floating window on it, which is every program this
desktop exists to host. Almost certainly the same cause as "firefox is now a black screen window".

`pc-shell-start` already knew: it runs `fullscreen disable` on the shell right after the window
appears and says why in a comment. Creating the window with `fullscreen: true` made that a RACE the
flag could win, silently. The shell fills the display by being TILED — which is what that script's
`floating disable` arranges, and a tiled window has floating windows above it, which is the stacking
a desktop needs.

Two levels are checked, because the second is what survives F11 and a compositor restoring a
remembered state: the window is not created fullscreen, and the sync drops the state if it ever
finds it while native windows are open.
"""
import re
import unittest
from pathlib import Path

from tests.client.test_native_window_follows_its_frame import body, strip_comments

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "desktop" / "main.js"
OS_JS = ROOT / "static" / "js" / "client" / "os.js"
START = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-shell-start-wayfire"


class TheShellWindowIsNotCreatedFullscreen(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = strip_comments(MAIN.read_text())

    def test_shell_mode_does_not_ask_for_fullscreen(self):
        m = re.search(r"SHELL_MODE \? \{([^}]*)\}", self.src)
        self.assertIsNotNone(m, "the SHELL_MODE window options moved — re-read this test")
        self.assertNotIn("fullscreen", m.group(1),
                         "the shell asks the compositor for fullscreen, which hides every floating "
                         "window on the workspace — i.e. every app it is meant to host")

    def test_it_still_fills_the_screen(self):
        """Without fullscreen AND without this, a compositor that does not pin the window leaves the
        desktop as a 1280x860 box in the middle of the display.

        Matched as one guarded statement rather than by looking near the first `win.maximize()` —
        main.js already had one, restoring `cfg.maximized`, and anchoring on it passed while the
        shell had no maximise at all."""
        self.assertRegex(self.src, r"if \(SHELL_MODE\)[^\n]*created\.maximize\(\)",
                         "shell mode neither asks for fullscreen nor fills the screen")

    def test_the_pre_existing_restore_is_untouched(self):
        """A normal window still comes back maximised if it was left that way."""
        self.assertIn("cfg.maximized", self.src)


class TheSyncDropsItIfItComesBack(unittest.TestCase):
    """F11, a stray keybinding, or a restored session can put it back, and none of them announce it."""

    @classmethod
    def setUpClass(cls):
        cls.sync = body(strip_comments(OS_JS.read_text()), "async function nsync")

    def test_the_guard_exists(self):
        self.assertIn("_natShell.fullscreen", self.sync)
        self.assertIn("pcWM.fullscreen(", self.sync)

    def test_it_only_fires_when_something_is_hosted(self):
        """A desktop with no native windows is welcome to be fullscreen."""
        i = self.sync.index("_natShell.fullscreen")
        self.assertIn("nativeWins().length", self.sync[i:i + 160])

    def test_it_asks_for_false(self):
        i = self.sync.index("pcWM.fullscreen(")
        self.assertIn("false", self.sync[i:i + 60])


class TheStartScriptStillDisablesIt(unittest.TestCase):
    """Belt and braces, and the place the reasoning was first written down.

    The Sway launcher enforced the shell's stacking itself, with `fullscreen disable` and
    `floating disable` sent over its own IPC. Wayfire has no tiling to arrange: main.js assigns each
    shell surface to a whole output through `assignShell`, and the failsafe below is what clears a
    fullscreen state the compositor may restore. Those two assertions went with the compositor; what
    the launcher is still uniquely responsible for -- the session environment the shell and the
    portal both need -- is checked here.
    """

    def test_main_process_clears_fullscreen_if_renderer_timer_is_throttled(self):
        src = MAIN.read_text()
        handler = src.split("ipcMain.handle('pc:wm:fullscreen'", 1)[1]
        handler = handler[:handler.index("ipcMain.handle('pc:wm:snap'")]
        self.assertIn("_shellFullscreenFailsafes", src)
        self.assertIn("setTimeout", handler)
        self.assertIn("wm().fullscreen(n,false)", handler)
        self.assertIn("shellWindow", handler)
        self.assertIn("row.title", handler)

    def test_shell_mode_can_never_start_hidden(self):
        """A hidden normal desktop app is fine; a hidden OS shell is an empty compositor."""
        src = MAIN.read_text()
        self.assertIn('startHidden = !SHELL_MODE && background.launchedHidden()', src)

    def test_recovery_launch_recovers_the_wayland_display(self):
        """A WAYFIRE SOCKET IS NOT A WAYLAND DISPLAY, and only the second one starts Electron.

        The launcher refuses without WAYFIRE_SOCKET, which made it look as though the environment
        had been checked. WAYLAND_DISPLAY is separate and can be absent on a recovery or ssh-driven
        restart -- and unset, everything still reports success (an empty value imports into systemd
        quite happily) until Chromium dies with "Failed to connect to Wayland display" and both
        launch attempts are spent on it. Find the socket rather than assume a name, and refuse
        rather than launch into that error.
        """
        src = START.read_text()
        self.assertIn("-name 'wayland-*'", src)
        self.assertIn('WAYLAND_DISPLAY=${wayland_socket##*/}', src)
        self.assertIn('export WAYLAND_DISPLAY', src)
        self.assertIn('could not find', src, "an unfindable display must refuse, not launch")
        self.assertIn('XDG_SESSION_TYPE=wayland', src)
        self.assertIn('XDG_CURRENT_DESKTOP=wayfire', src)

    def test_recovery_launch_repairs_the_portal_service_environment(self):
        """`grim` talks to the compositor directly and can work while OBS lists zero screens. The
        latter means the already-running systemd portal never received the session's Wayland
        variables -- it is a --user service and can start before anything exports them."""
        src = START.read_text()
        export_at = src.index('export XDG_SESSION_TYPE=wayland')
        import_at = src.index('systemctl --user import-environment')
        launch_at = src.index('"$launcher" --shell')
        self.assertLess(export_at, import_at)
        self.assertLess(import_at, launch_at)
        self.assertIn('dbus-update-activation-environment --systemd', src)
        self.assertIn('try-restart xdg-desktop-portal-wlr.service xdg-desktop-portal.service', src)
        self.assertIn('portal_stale', src, "every shell restart would interrupt healthy capture")


if __name__ == "__main__":
    unittest.main()
