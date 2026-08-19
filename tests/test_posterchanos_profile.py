"""The PosterChanOS profile in os/gentoo.sh — asserted, because every one of these fails LATE.

A missing package here does not fail the build. It fails the first time somebody presses record, or
plugs in a second monitor, or launches a game — and on an OS install that is hours after the mistake
was made. So the profile is checked as a list of requirements with reasons, not eyeballed.

What this cannot do is build Gentoo. It reads the script.
"""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = os.path.join(ROOT, "os", "gentoo.sh")


@unittest.skipIf(not os.path.exists(SH), "no os/gentoo.sh here")
class PosterChanOSProfile(unittest.TestCase):
    def setUp(self):
        self.src = open(SH, encoding="utf-8").read()
        m = re.search(r'POSTERCHANOS_PACKAGES="(.*?)"', self.src, re.S)
        self.assertTrue(m, "the profile's package list moved — re-point this test")
        self.pkgs = set(m.group(1).replace("\\\n", " ").split())

    def test_the_script_is_valid_shell(self):
        r = subprocess.run(["bash", "-n", SH], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])

    def test_no_password_left_in_it(self):
        """A copy of somebody's installer lives in a public mirror. Every credential in it is a
        placeholder or it does not belong here."""
        for line in self.src.splitlines():
            m = re.match(r'^(USER_PASSWORD|ROOT_PASSWORD|WIRELESS_PASSWORD|DISK_PASSWORD|SSID)='
                         r'''["']?([^"'#]*)''', line)
            if m:
                self.assertEqual(m.group(2).strip(), "123456",
                                 f"{m.group(1)} is not a placeholder")

    def test_there_is_a_compositor_and_xwayland(self):
        """XWayland is not optional the moment Steam is in scope — most games, and Steam's own
        client, are X11 clients and simply have no way onto the screen without it."""
        self.assertIn("gui-wm/sway", self.pkgs)
        self.assertIn("x11-base/xwayland", self.pkgs)

    def test_screen_capture_has_all_three_halves(self):
        """Wayland has no "read the screen" call by design, so a recorder gets frames through the
        ScreenCast portal over PipeWire. The front end, the wlroots BACK end and PipeWire must all be
        there: with the front end alone the portal answers "no such capture" and OBS shows a screen
        capture source that lists nothing to capture — at the moment somebody presses record."""
        for pkg in ("sys-apps/xdg-desktop-portal", "gui-libs/xdg-desktop-portal-wlr",
                    "media-video/pipewire", "media-video/obs-studio"):
            self.assertIn(pkg, self.pkgs, f"{pkg} missing — screen recording dies at record time")

    def test_obs_is_built_with_pipewire(self):
        """An OBS without the flag builds, installs, runs, and has no PipeWire capture source in it.
        Nothing about that looks like a packaging mistake from the desktop."""
        m = re.search(r'SPECIAL_PACKAGE_USE=\((.*?)\n\)', self.src, re.S) \
            or re.search(r'SPECIAL_PACKAGE_USE=\((.*?)\)\n', self.src, re.S)
        self.assertTrue(m, "the USE table moved")
        self.assertIn("media-video/obs-studio", m.group(1))
        obs = re.search(r'"media-video/obs-studio ([^"]*)"', m.group(1))
        self.assertTrue(obs and "pipewire" in obs.group(1),
                        "obs-studio is not built with pipewire — no screen capture on Wayland")

    def test_the_portal_backend_is_named_for_this_desktop(self):
        """The portal picks its backend by desktop NAME and has no fallback: an unknown name gets
        "no such capture", which reads to the person as OBS being broken."""
        self.assertIn("org.freedesktop.impl.portal.ScreenCast=wlr", self.src)

    def test_kde_is_not_installed_in_this_profile(self):
        """"be light as possible" — the shell IS the desktop, so a second desktop environment is
        pure cost: plasma-meta and the kde-apps set are most of the disk and nearly all of the build
        time on a source distribution."""
        for heavy in ("plasma-meta", "kde-apps/", "kde-plasma/"):
            self.assertFalse([p for p in self.pkgs if heavy in p],
                             f"{heavy} is in the PosterChanOS profile")

    def test_the_profile_replaces_the_desktop_apps_rather_than_adding_to_them(self):
        """Appending would install the minimal stack ON TOP of Plasma — twice the desktop and none of
        the saving, which is exactly the mistake that looks like it worked."""
        m = re.search(r'if \[\[ "\$POSTERCHANOS" = \*y\* \]\]; then\s*\n\s*'
                      r'PACKAGES="([^"]*)"', self.src)
        self.assertTrue(m, "the profile switch moved")
        self.assertNotIn("DESKTOP_APPS", m.group(1),
                         "the KDE app list is still installed under PosterChanOS")

    def test_flatpak_is_not_used_by_this_profile(self):
        """Its only real customer was Steam, which portage builds natively — and a flatpak runtime is
        a second copy of most of a graphics stack."""
        m = re.search(r'if \[\[ "\$POSTERCHANOS" = \*y\* \]\];.*?\nfi', self.src, re.S)
        self.assertTrue(m)
        self.assertIn('FLATPAK_PACKAGES=""', m.group(0))

    def test_the_shell_is_started_and_windows_are_placeable(self):
        """A TILED window ignores position and size — PosterChan would move things and nothing would
        happen, silently. The desktop places windows, so they have to be floating."""
        self.assertIn("posterchan --shell", self.src)
        self.assertIn("floating enable", self.src)

    def test_the_compositor_draws_no_chrome(self):
        """PosterChan draws the window frame, so sway must not draw one too. Left on, its borders and
        title bars sit on top of the PosterChan desktop wearing the wrong font — two window styles on
        one screen, and the seam is exactly the thing that makes a shell look like a hack."""
        for rule in ("default_border none", "default_floating_border none"):
            self.assertIn(rule, self.src, f"sway still draws {rule.split()[0]}")

    def test_nothing_paints_over_the_desktop_uninvited(self):
        """PosterChan IS the wallpaper and the taskbar. A compositor wallpaper underneath is invisible
        and a status bar on top is a second one."""
        self.assertIn("output * bg", self.src)
        self.assertNotIn("swaybar", self.src)
        self.assertNotIn("bar {", self.src)

    def test_the_marker_is_written_before_the_package_step(self):
        """install-packages runs INSIDE the chroot, where an environment variable does not reach. If
        the marker is only written by finalizeInstall, the chroot rebuilds the default list and
        installs the entire KDE desktop — hours of it — on the profile whose point is not having
        one, and nothing about that looks like a mistake until it finishes."""
        i = self.src.index("gentoo.sh install-packages")
        before = self.src[:i]
        j = before.rindex("buildGentoo() {")
        self.assertIn("touch $TARGET/etc/posterchanos", before[j:],
                      "the profile marker is not written before the chroot package step")

    def test_the_gentoo_profile_is_not_the_plasma_one(self):
        """The Gentoo PROFILE is chosen before any package list is consulted, and desktop/plasma
        turns on the KDE USE flags system-wide — it pulls Plasma into @world whatever PACKAGES says.
        A "minimal" build was caught emerging kde-frameworks/breeze-icons because of this line, and
        nothing in the package list could have prevented it."""
        i = self.src.index("GENTOO_PROFILE=")
        block = self.src[max(0, i - 700):i + 700]
        self.assertIn("POSTERCHANOS", block,
                      "the profile is picked without consulting PosterChanOS — it gets Plasma")
        pcos = [ln for ln in block.splitlines()
                if "GENTOO_PROFILE=" in ln and "-vi 'plasma" in ln]
        self.assertTrue(pcos, "the PosterChanOS branch does not exclude the plasma profile")

    def test_the_default_profile_is_unchanged(self):
        """This is somebody's working installer for their own machines. PosterChanOS is opt-in, and a
        plain run must still build exactly what it built before."""
        self.assertIn('PACKAGES="$BASE_PACKAGES $DESKTOP_APPS"', self.src)
        self.assertIn('POSTERCHANOS="${POSTERCHANOS:-n}"', self.src)
        self.assertIn("/etc/posterchanos", self.src,
                      "the profile cannot survive the chroot the package step runs in")


if __name__ == "__main__":
    unittest.main()
