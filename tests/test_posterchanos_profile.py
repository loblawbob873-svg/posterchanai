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

    def _fn(self, name):
        """The whole shell function, by brace matching.

        Every one of these checks used a fixed-size window into the file, and every time the
        function grew past it a test failed for a reason that had nothing to do with what it checks.
        A test that fails for the wrong reason teaches people to edit the test."""
        i = self.src.index(name + "() {")
        depth, k = 0, self.src.index("{", i)
        while k < len(self.src):
            if self.src[k] == "{":
                depth += 1
            elif self.src[k] == "}":
                depth -= 1
                if depth == 0:
                    return self.src[i:k + 1]
            k += 1
        raise AssertionError(f"{name}: unbalanced braces")

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

    def test_nobody_is_baked_into_the_image(self):
        """Accounts are made when somebody signs in with a key, so a named human in the installer is
        wrong twice over: it is not their machine, and it is the account every copy of the image
        would share. What must exist is a session to run the shell in BEFORE anyone has signed in."""
        acc = self._fn("accounts")
        i = acc.index("$POSTERCHANOS")
        pcos = acc[i:acc.index("return 0", i)]
        self.assertIn('SHELL_USER="posterchan"', pcos)
        self.assertNotIn("$USER", pcos, "the PosterChanOS branch still creates a named account")

    def test_the_session_account_cannot_be_logged_into(self):
        """It is reached by autologin and must not be a way IN from anywhere else — not ssh, not a
        login prompt, not su."""
        acc = self._fn("accounts")
        i = acc.index("$POSTERCHANOS")
        self.assertIn("passwd -l $SHELL_USER", acc[i:acc.index("return 0", i)])

    def test_root_keeps_a_password_on_this_profile(self):
        """The default path locks root, which is defensible when one named human has NOPASSWD sudo
        and catastrophic here, where nobody does. Measured the hard way: sudo refused a sudoers file
        it had been handed at the wrong mode, root was locked, and the only way back into a freshly
        installed machine was editing the kernel command line at the boot menu."""
        acc = self._fn("accounts")
        i = acc.index("$POSTERCHANOS")
        pcos = acc[i:acc.index("return 0", i)]
        self.assertIn('echo "root:$ROOT_PASSWORD" | chpasswd', pcos)
        self.assertNotIn("passwd -dl root", pcos, "root is locked with nobody able to sudo")

    def test_the_session_account_is_not_an_administrator(self):
        acc = self._fn("accounts")
        i = acc.index("$POSTERCHANOS")
        pcos = acc[i:acc.index("return 0", i)]
        self.assertNotIn("NOPASSWD: ALL", pcos, "the shell account was given blanket sudo")
        self.assertIn("NOPASSWD: /usr/local/bin/pc-provision-user", pcos)

    def test_anyone_can_be_given_an_account_but_not_root(self):
        """Anyone may sign in, so an account must exist before they have anywhere to put anything.
        The sudoers rule is limited to that ONE command: signing in with a key is not the same as
        being trusted with root, and a machine anyone may log into must not hand every visitor
        sudo."""
        body = self._fn("posterchanShell")
        self.assertIn("pc-provision-user", body, "nothing provisions an account for a new identity")
        self.assertIn("sudoers.d", body, "the shell cannot create an account it is not allowed to")
        rule = [l for l in body.splitlines() if "NOPASSWD" in l]
        self.assertTrue(rule, "no sudoers rule")
        self.assertTrue(all("pc-provision-user" in l and "ALL=(root) NOPASSWD: /usr/local/bin/" in l
                            for l in rule),
                        f"the sudoers rule is broader than one command: {rule}")

    def test_sudoers_drop_ins_are_actually_read(self):
        """`accounts()` writes /etc/sudoers wholesale, which drops the line that makes
        /etc/sudoers.d readable at all — so every drop-in rule is silently ignored, including the one
        that lets the shell provision an account. Nothing reports it: a sudoers.d file that is never
        read looks exactly like one that is."""
        acc = self._fn("accounts")
        self.assertIn("includedir /etc/sudoers.d", acc,
                      "/etc/sudoers is rewritten without its includedir — sudoers.d is dead")

    def test_sudoers_is_left_with_the_mode_sudo_demands(self):
        """sudo refuses to run AT ALL unless /etc/sudoers is 0440 root:root, and `echo >` creates it
        with the default umask when the file does not already exist — which is exactly what happens
        when this runs before app-admin/sudo is installed. Root is locked two lines later, so the
        result is a machine with no way in except editing the kernel command line. Measured on a real
        install: mode 0644, "sudo: no valid sudoers sources found, quitting"."""
        acc = self._fn("accounts")
        self.assertIn("chmod 0440 /etc/sudoers", acc, "sudo will refuse the file it just wrote")
        self.assertIn("chown root:root /etc/sudoers", acc)
        # The COMMAND, not the phrase — the PosterChanOS branch mentions it in prose to explain why
        # it does not do it, and a test that matches prose is a test about the comments.
        self.assertLess(acc.index("chmod 0440 /etc/sudoers"), acc.index("/usr/bin/passwd -dl root"),
                        "sudoers is fixed after root is locked — if it fails there is no way back")

    def test_sound_is_enabled_for_users_that_do_not_exist_yet(self):
        """Gentoo ships the PipeWire user services disabled, and the usual fix is `systemctl --user`,
        which acts on the account running it and nothing else. Accounts here are created when
        somebody signs in with a key — long after the installer has finished — so each would come up
        silent with no obvious reason why. `--global` writes it where every future session sees it."""
        body = self._fn("posterchanShell")
        self.assertIn("systemctl --global enable", body, "sound is enabled per-user, or not at all")
        self.assertIn("wireplumber.service", body)
        self.assertIn("pipewire-pulse.socket", body)
        self.assertNotIn("systemctl --user enable", body,
                         "a per-user enable cannot reach an account that does not exist yet")

    def test_power_and_media_need_no_extra_packages(self):
        """Every one of these was a package I added and then removed once I looked at what the
        kernel already exposes: brightness is /sys/class/backlight (and brightnessctl is not even in
        the Gentoo tree), power profiles are /sys/firmware/acpi/platform_profile with cpufreq
        governors behind them, and MPRIS is reachable with busctl, which systemd already ships.
        Fewer packages on a machine somebody else runs is worth more than the abstractions."""
        for gone in ("brightnessctl", "power-profiles-daemon", "playerctl"):
            self.assertFalse([p for p in self.pkgs if gone in p],
                             f"{gone} is back in the profile — check whether it is really needed")

    def test_the_super_key_reaches_the_shell_from_inside_another_app(self):
        """A start menu you cannot open while a browser is focused is not one, and that is exactly
        the machine state you press Super in. The shell's own key handler cannot see the press —
        the compositor gave the keyboard to firefox — so sway broadcasts a tick instead.

        `--release` is the load-bearing flag: a binding on the PRESS swallows Super, and every
        `$mod+…` shortcut on the machine stops working. Both copies of the config are checked,
        because the installer writes one and the overlay package ships the other, and a binding in
        only one of them works on exactly half the installs."""
        overlay = os.path.join(ROOT, "os", "overlay", "app-misc", "posterchanos-shell",
                               "files", "sway.config")
        texts = {"os/gentoo.sh": self.src}
        if os.path.exists(overlay):
            texts["overlay sway.config"] = open(overlay, encoding="utf-8").read()
        for where, text in texts.items():
            line = [ln for ln in text.splitlines()
                    if "send_tick" in ln and "pc:start" in ln]
            self.assertTrue(line, f"{where} has no Super binding — the start menu cannot be "
                                  f"opened from inside another app")
            self.assertIn("--release", line[0],
                          f"{where} binds Super on the PRESS, which swallows it and breaks "
                          f"every $mod+key shortcut")

    def test_the_backlight_is_writable_without_root(self):
        """sysfs is root-owned, so a session can read the brightness and not change it — a slider
        that moves and does nothing. The udev rule hands it to the `video` group, which
        pc-provision-user already puts every account in."""
        body = self._fn("posterchanShell")
        self.assertIn("SUBSYSTEM==\"backlight\"", body, "the backlight is root-only")
        self.assertIn("chgrp video", body)

    def test_no_html_engine_can_be_built_from_source(self):
        """webkit-gtk and qtwebengine are among the longest builds in the tree, and the way you find
        out something pulled one is that an install which looked nearly finished sits on a single
        package all night. A mask turns that into an error at dependency-resolution time, naming
        whatever asked for it. The browser here is firefox-BIN, which is prebuilt."""
        body = self._fn("posterchanShell")
        self.assertIn("package.mask", body, "nothing stops a dependency pulling an HTML engine")
        for heavy in ("net-libs/webkit-gtk", "dev-qt/qtwebengine", "www-client/chromium"):
            self.assertIn(heavy, body, f"{heavy} is not masked")

    def test_the_overlay_is_registered_and_preferred(self):
        """An install that came from the overlay is one that can be UPDATED — `emerge -u
        app-misc/posterchan-desktop` instead of somebody re-running an installer."""
        repo = self._fn("gentooRepo")
        self.assertIn("[posterchan]", repo, "the overlay is never registered")
        self.assertIn("posterchan-overlay.git", repo)
        self.assertIn("sync-type = git", repo, "portage cannot sync a directory over https")
        body = self._fn("posterchanShell")
        self.assertIn("emerge --sync posterchan", body, "the overlay is registered but never used")
        self.assertIn("posterchanos-shell", body)

    def test_the_manual_path_survives_as_a_fallback(self):
        """A first install is exactly when the overlay might not be reachable: no network yet, a
        mirror being rebuilt, a machine provisioned before the repo was published."""
        body = self._fn("posterchanShell")
        self.assertIn("not reachable", body, "an unreachable overlay leaves no way to install")
        self.assertIn("AppImage", body, "the direct install was removed with the overlay added")

    def test_overlay_success_is_checked_by_looking_not_by_exit_code(self):
        """A package that installs nothing useful exits 0."""
        body = self._fn("posterchanShell")
        i = body.index("emerge --sync posterchan")
        self.assertIn('-x "${TARGET}/usr/local/bin/posterchan"', body[i:i + 1200],
                      "the overlay install is trusted rather than verified")

    def test_emerge_sync_works_off_this_lan(self):
        """rsync://gentoo-repo.lan resolves on exactly one network. An OS other people run cannot be
        pointed at a .lan name, and the way they find out is that the machine can never update."""
        repo = self._fn("gentooRepo")
        # ONLY the PosterChanOS arm — the else branch keeps the LAN rsync for the default profile,
        # which is somebody's own machines on their own network and is correct there.
        i = repo.index("$POSTERCHANOS")
        pcos = repo[i:repo.index("else", i)]
        self.assertIn("sync-type = webrsync", pcos)
        self.assertIn("https://gentoo.poster.place", pcos)
        self.assertNotIn(".lan", pcos, "PosterChanOS still syncs from a LAN-only host")
        self.assertIn("sync-webrsync-verify-signature = true", pcos,
                      "a package tree fetched over HTTP from somebody's server, unverified")

    def test_the_shell_itself_is_installed(self):
        """sway's config execs `posterchan`. Nothing else in this installer puts it on the disk, so
        without this step the machine boots into an empty compositor with no way to do anything —
        the most convincing possible imitation of a broken install."""
        body = self._fn("posterchanShell")
        self.assertIn("AppImage", body, "the desktop is never installed")
        self.assertIn("/usr/local/bin/posterchan", body, "nothing provides the `posterchan` command")
        self.assertIn("appimage-extract", body,
                      "an AppImage run as one needs FUSE, which a minimal profile does not have")
        self.assertIn("no shell", body, "a failed install is silent — sway starts with nothing")

    def test_the_shell_is_started_through_the_launcher(self):
        """`for_window` cannot be relied on for this window: an X11 client sets WM_CLASS AFTER it
        maps, so sway evaluates criteria against a window with no class yet — every rule looks right
        in the file, none of them match, and the shell floats at 1280x860 in the middle of the
        screen. The launcher finds the window first and pins it second, which is the same order
        wm.js uses for anything it launches."""
        self.assertIn("pc-shell-start", self.src, "the shell is started without pinning its window")
        cfg = self._fn("posterchanShell")
        # The catch-all float rule IS wanted — see test_everything_else_floats_above_it — but the
        # shell must be excluded by a later rule, or it floats at 1280x860 in the middle of the
        # screen. Both halves, or neither works.
        self.assertIn('for_window [app_id=".*"] floating enable', cfg)
        i = cfg.index('for_window [app_id=".*"] floating enable')
        self.assertIn('for_window [app_id="posterchan-desktop"] floating disable', cfg[i:],
                      "the shell is floated by the catch-all and never un-floated — later rules win, "
                      "so the exclusion has to come after")

    def test_the_shell_is_tiled_and_never_fullscreen(self):
        """A fullscreen window in sway covers the whole workspace INCLUDING floating windows. With
        the shell fullscreen a terminal opens, exists, reports its geometry and is `visible: false` —
        nothing on screen, no error, and no way to get a terminal on the machine. Being the only
        TILED window fills the screen just the same and lets everything float above it, which is what
        the desktop is for."""
        p = os.path.join(ROOT, "os", "bin", "pc-shell-start")
        body = open(p, encoding="utf-8").read()
        self.assertIn("fullscreen disable", body, "the shell pins itself fullscreen")
        self.assertNotIn("fullscreen enable", body)
        self.assertIn("floating disable", body, "the shell must be the tiled one")

    def test_everything_else_floats_above_it(self):
        """Without the float rules every app TILES, and tiling against the shell gives the newcomer
        zero space: Firefox launches, appears in the tree, and is 0x0."""
        cfg = self._fn("posterchanShell")
        self.assertIn('for_window [app_id=".*"] floating enable', cfg)
        self.assertIn('for_window [app_id="posterchan-desktop"] floating disable', cfg)

    def test_the_launcher_waits_for_the_window_before_pinning_it(self):
        p = os.path.join(ROOT, "os", "bin", "pc-shell-start")
        self.assertTrue(os.path.exists(p), "the launcher is not shipped")
        body = open(p, encoding="utf-8").read()
        self.assertIn("get_tree", body, "it pins whatever is there rather than waiting for ours")
        for spelling in ('class="posterchan-desktop"', 'app_id="posterchan-desktop"'):
            self.assertIn(spelling, body,
                          "only one of app_id/class is handled — the other silently does nothing")

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

    def test_steam_is_opt_in_and_not_in_the_base_profile(self):
        """Steam is a separate step and always was. Its tooling belongs with it: gamescope is a
        micro-compositor for GAMES and has no business being emerged on a machine that never
        installs Steam."""
        for game in ("steam", "gamescope", "wine"):
            self.assertFalse([p for p in self.pkgs if game in p],
                             f"{game} is in the always-installed profile")
        steam = self._fn("installSteam")
        self.assertIn("gamescope", steam, "gamescope is not installed with Steam")
        # ...and it must not drag a multilib world rebuild in with it. Native steam-launcher pulls
        # ABI_X86=32 through the whole graphics stack — every library built twice, hours of it, for
        # a program that ships its own runtime.
        self.assertIn("com.valvesoftware.Steam", steam,
                      "PosterChanOS builds Steam natively — that is the 32-bit stack from source")
        self.assertIn("$POSTERCHANOS", steam, "the flatpak path is not gated on the profile")

    def test_every_package_name_has_a_category(self):
        """`games-util/gamescope` does not exist — it is `gui-wm/gamescope` — and emerge refuses the
        ENTIRE set for one unresolvable atom. That typo installed a kernel, a shell session and a
        portal config, and no sway, no browser and no OBS, while the install reported success. A
        category check cannot know what exists in the tree, but it can insist every entry looks like
        an atom rather than a bare name."""
        for pkg in self.pkgs:
            self.assertRegex(pkg, r"^[a-z0-9-]+/[A-Za-z0-9._+-]+$", f"{pkg!r} is not an atom")

    def test_a_bad_package_name_cannot_cost_the_whole_desktop(self):
        """emerge is all-or-nothing for a set, and nothing checked its exit — buildGentoo carried
        straight on to finalizeInstall. The retry names what failed, which is the difference between
        "the desktop is missing" and "these two names are wrong"."""
        body = self._fn("installPackages")
        self.assertIn("FAILED_PKGS", body, "a failed package set is still silent")
        self.assertIn("for pkg in $PACKAGES", body, "there is no per-package retry")
        self.assertIn("return 1", body, "the failure is not reported to the caller")

    def test_the_installer_can_be_driven_without_the_menu(self):
        """Driving the menu from a pipe worked until the input ran out: `read` returned instantly,
        `menu` recursed on every empty answer, and bash died of a stack overflow at the end of an
        hour-long install."""
        for arg in ('"$1" = "download"', '"$1" = "build"', '"$1" = "install"'):
            self.assertIn(arg, self.src, f"no unattended entry point for {arg}")

    def test_the_default_profile_is_unchanged(self):
        """This is somebody's working installer for their own machines. PosterChanOS is opt-in, and a
        plain run must still build exactly what it built before."""
        self.assertIn('PACKAGES="$BASE_PACKAGES $DESKTOP_APPS"', self.src)
        self.assertIn('POSTERCHANOS="${POSTERCHANOS:-n}"', self.src)
        self.assertIn("/etc/posterchanos", self.src,
                      "the profile cannot survive the chroot the package step runs in")


if __name__ == "__main__":
    unittest.main()
