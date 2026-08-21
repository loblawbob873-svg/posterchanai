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

    def test_the_session_environment_reaches_systemd_or_there_is_no_screen_capture(self):
        """THE ONE THAT MADE OBS LIST NOTHING, with every package correctly installed.

        xdg-desktop-portal is a SYSTEMD USER SERVICE. It does not inherit the sway session's
        environment — it inherits `systemd --user`'s, which is empty of it — so it comes up with no
        XDG_CURRENT_DESKTOP, matches neither `sway-portals.conf` nor `UseIn=…;sway;…` in wlr.portal,
        loads no backend, and exposes no ScreenCast interface at all. Measured on the test laptop:
        `No such interface "org.freedesktop.portal.ScreenCast"` with xdg-desktop-portal-wlr
        installed, pipewire running and OBS 32 ready. It reads as an OBS problem and is not one; the
        desktop app's own file dialog fails identically in the same breath.

        Both calls are asserted, because they fill DIFFERENT environments and a session with one of
        them works for half the things that need it: `import-environment` is systemd's user manager
        (which starts the portal), `dbus-update-activation-environment` is the D-Bus activation
        environment (which starts anything D-Bus launches directly)."""
        for where, text in self._sway_configs().items():
            self.assertIn("systemctl --user import-environment", text,
                          f"{where}: the session environment never reaches systemd, so the portal "
                          f"loads no backend and screen capture does not exist")
            self.assertIn("dbus-update-activation-environment", text,
                          f"{where}: the D-Bus activation environment is never filled in")
            self.assertIn("XDG_CURRENT_DESKTOP", text,
                          f"{where}: the desktop NAME is what selects the portal backend, and it "
                          f"is not among the variables being carried over")

    # ── EVERY PROGRAM THE SHELL RUNS IS INSTALLED ──────────────────────────────────────────────
    #
    # The shell learns everything it knows about this machine by running a command, and a missing
    # one does not crash it — the bridge returns a refusal, which reads to the person using it as a
    # control that does nothing at all. Two of these were live: `brightnessctl` is what
    # desktop/power.js falls back to whenever /sys/class/backlight is root-owned, and it was in no
    # list, so on such a machine the brightness slider moved and changed nothing; `grim` is the
    # whole of the screenshot feature and was the same shape.
    #
    # THE POINT OF DOING IT THIS WAY is that the list is derived from the CODE. Both of those tools
    # were present on the test laptop as somebody else's dependency, so nothing was visibly wrong
    # there — the failure only appears on the next fresh build, which is hours of emerge away from
    # the mistake. So this reads desktop/*.js for the binaries it actually invokes and asserts each
    # one has a package, rather than checking a list against another list.
    #
    # `base` is the things @system and BASE_PACKAGES already carry. They are exempt from needing a
    # POSTERCHANOS_PACKAGES entry, but NOT exempt from being known about: an unrecognised binary
    # appearing in desktop/*.js fails this test, which is the whole mechanism.
    PKG_FOR = {
        "grim": "gui-apps/grim",
        "slurp": "gui-apps/slurp",
        "wl-copy": "gui-apps/wl-clipboard",
        # Same package. `wl-paste` is how a screenshot's "· copied" claim is CHECKED — Electron's
        # own clipboard.writeImage does not take the Wayland selection here, and readImage() cannot
        # prove otherwise because Chromium hands back its own cached write. It is allowed to be a
        # subprocess where wl-copy is not, because it EXITS: a child of the shell inherits
        # Chromium's non-CLOEXEC descriptors, so a daemon holds them for ever and a short-lived
        # process does not.
        "wl-paste": "gui-apps/wl-clipboard",
        "wpctl": "media-video/wireplumber",
        # DELIBERATELY NOT PACKAGED, and this entry is the record of why: it is not in the Gentoo
        # tree, so adding it breaks emerge on every fresh build. desktop/power.js only reaches for
        # it when /sys/class/backlight is root-owned, and what makes that path unnecessary here is
        # the udev rule that hands the backlight to the `video` group. Adding the package is the
        # obvious wrong fix for a brightness slider that does nothing;
        # test_power_and_media_need_no_extra_packages is what stops it.
        "brightnessctl": "none:not in the Gentoo tree — the udev rule covers this instead",
        "xdg-open": "x11-misc/xdg-utils",
        "swaymsg": "gui-wm/sway",
        "nmcli": "base:net-misc/networkmanager",   # BASE_PACKAGES
        "systemctl": "base:sys-apps/systemd",
        "script": "base:sys-apps/util-linux",      # the local terminal's PTY
        "sudo": "base:app-admin/sudo",
        # Windows-only, and never run on this profile.
        "attrib": "base:n/a", "icacls": "base:n/a",
    }

    def _binaries_the_shell_runs(self):
        """Every external program desktop/*.js invokes, read out of the source."""
        d = os.path.join(ROOT, "desktop")
        found = set()
        for name in sorted(os.listdir(d)):
            if not name.endswith(".js"):
                continue
            src = open(os.path.join(d, name), encoding="utf-8").read()
            # spawn('x', …) / execFile('x', …) / run('x', …)
            found |= set(re.findall(r"(?:execFile|execFileSync|spawnSync|spawn|run)\(\s*'([a-z][a-z0-9._-]*)'", src))
            # const GRIM = process.env.PC_GRIM || 'grim';
            found |= set(re.findall(r"process\.env\.PC_[A-Z_]+\s*\|\|\s*'([a-z][a-z0-9._-]*)'", src))
        return found

    def test_every_program_the_shell_runs_has_a_package(self):
        for binary in sorted(self._binaries_the_shell_runs()):
            with self.subTest(binary=binary):
                pkg = self.PKG_FOR.get(binary)
                self.assertIsNotNone(
                    pkg,
                    "desktop/*.js runs `%s`, and nothing in this test knows which package provides "
                    "it. Add it to PKG_FOR and to os/gentoo.sh — a tool with no package is a "
                    "control that silently does nothing on a fresh build." % binary)
                if pkg.startswith("base:") or pkg.startswith("none:"):
                    continue
                self.assertIn(pkg, self.pkgs,
                              "the shell runs `%s`, so %s has to be in POSTERCHANOS_PACKAGES"
                              % (binary, pkg))

    def test_the_screenshot_tools_are_installed(self):
        """Named on their own because this is a whole feature, not a fallback path: with no grim
        there is no screenshot at all, and with no slurp there is no way to pick an area."""
        for pkg in ("gui-apps/grim", "gui-apps/slurp"):
            self.assertIn(pkg, self.pkgs, pkg + " is what takes a screenshot on wlroots")

    def test_something_answers_the_FileChooser_portal(self):
        """The wlroots backend implements ScreenCast and nothing else, so with it alone there is no
        FileChooser on the bus — measured, and it is what Folder Sync's "choose a folder" needs."""
        self.assertIn("xdg-desktop-portal-gtk", self.src,
                      "no portal backend provides FileChooser, so no file dialog can open")

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
        m = re.search(r'^PACKAGES="([^"]*)"', self.src, re.M)
        self.assertTrue(m, "PACKAGES moved — re-read this test")
        self.assertEqual(m.group(1), "$BASE_PACKAGES $POSTERCHANOS_PACKAGES")

    def test_flatpak_is_not_used_by_this_profile(self):
        """Its only real customer was Steam, which portage builds natively — and a flatpak runtime is
        a second copy of most of a graphics stack. The list it used to carry was the KDE desktop's —
        konsole, dolphin, kcalc, kdenlive — and this OS supplies its own equivalents."""
        self.assertNotIn("FLATPAK_PACKAGES", self.src, "the flatpak app list came back")
        fp = self._fn("installFlatpaks")
        self.assertIn("remote-add", fp, "flathub should still be available to a person")
        self.assertNotIn("flatpak install", fp, "the installer installs flatpak apps again")

    def test_nobody_is_baked_into_the_image(self):
        """Accounts are made when somebody signs in with a key, so a named human in the installer is
        wrong twice over: it is not their machine, and it is the account every copy of the image
        would share. What must exist is a session to run the shell in BEFORE anyone has signed in."""
        acc = self._fn("accounts")
        pcos = "\n".join(l for l in acc.splitlines() if not l.lstrip().startswith("#"))
        self.assertIn('SHELL_USER="posterchan"', pcos)
        self.assertNotIn("$USER", pcos, "the PosterChanOS branch still creates a named account")

    def test_the_session_account_cannot_be_logged_into(self):
        """It is reached by autologin and must not be a way IN from anywhere else — not ssh, not a
        login prompt, not su."""
        acc = self._fn("accounts")
        self.assertIn("passwd -l $SHELL_USER", acc)

    def test_root_keeps_a_password_on_this_profile(self):
        """The default path locks root, which is defensible when one named human has NOPASSWD sudo
        and catastrophic here, where nobody does. Measured the hard way: sudo refused a sudoers file
        it had been handed at the wrong mode, root was locked, and the only way back into a freshly
        installed machine was editing the kernel command line at the boot menu."""
        # CODE ONLY. The comment above this function explains WHY root keeps a password by quoting
        # the very command that would lock it, and an assertion that reads prose fails on an
        # explanation of itself.
        acc = self._fn("accounts")
        code = "\n".join(l for l in acc.splitlines() if not l.lstrip().startswith("#"))
        self.assertIn('echo "root:$ROOT_PASSWORD" | chpasswd', code)
        self.assertNotIn("passwd -dl root", code, "root is locked with nobody able to sudo")

    def test_the_session_account_is_not_an_administrator(self):
        acc = self._fn("accounts")
        pcos = "\n".join(l for l in acc.splitlines() if not l.lstrip().startswith("#"))
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
        # This used to assert that sudoers was fixed BEFORE root was locked, because a failure
        # between the two left no way into the machine. Root is never locked now — the profile that
        # did it is gone — so the stronger statement is available: the command is not there at all.
        # Code only; the comment above the function names it to explain why it is absent.
        code = "\n".join(l for l in acc.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("passwd -dl root", code,
                         "root is locked on a profile where nobody has sudo — the only way back "
                         "into the machine is editing the kernel command line at the boot menu")

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

    def _sway_configs(self):
        """Both copies: the installer writes one and the overlay package ships the other, and a
        line in only one of them works on exactly half the installs."""
        out = {"os/gentoo.sh": self.src}
        overlay = os.path.join(ROOT, "os", "overlay", "app-misc", "posterchanos-shell",
                               "files", "sway.config")
        if os.path.exists(overlay):
            out["overlay sway.config"] = open(overlay, encoding="utf-8").read()
        return out

    def test_the_power_mode_can_be_CHANGED_and_not_only_read(self):
        """A control that reports a reading and refuses to change it is worse than no control: it
        looks like the feature is there. /sys/firmware/acpi/platform_profile is root:root 0644, so
        the panel could read that a laptop offers low-power/balanced/performance and select none of
        them.

        NOT a udev rule, and that is the point of the test: /sys/firmware/acpi is not a device and
        emits no `add` event, so a rule matching it never fires and the file stays root-only with
        the rule loaded and correct — which is exactly how the backlight version of this was got
        wrong once already. tmpfiles runs every boot regardless."""
        self.assertIn("platform_profile", self.src,
                      "nothing grants the power-mode file, so no profile can ever be selected")
        self.assertIn("tmpfiles", self.src,
                      "the power-mode grant is not tmpfiles — a udev rule cannot match sysfs paths "
                      "that emit no device event")
        # Applied at install time too, or the first session after an install has dead buttons.
        self.assertIn("systemd-tmpfiles --create", self.src,
                      "the grant is written but never applied, so it does nothing until a reboot")

    def test_every_masked_atom_is_one_portage_can_actually_parse(self):
        """`net-libs/webkit-gtk-6` is not a package — the GTK4/soup3 webkit is a SLOT of
        net-libs/webkit-gtk. Portage reads the trailing `-6` as a VERSION, a versioned atom is
        invalid without an operator, and the line masked NOTHING while printing `Invalid atom in
        /etc/portage/package.mask/posterchanos` on every portage command on the machine. Found by
        running a real `emerge --sync`, not by reading the file.

        The failure is the worst shape a guard can have: the mask exists, it looks right, it is
        installed, and the thing it forbids would have built anyway. "Avoid webkit entirely" was
        being enforced by three lines of which one was decoration."""
        start = self.src.index("<<-'MASK'") + len("<<-'MASK'")
        block = self.src[start:self.src.index("MASK", start)]
        atoms = [ln.strip() for ln in block.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        self.assertTrue(atoms, "the mask block could not be read")
        for atom in atoms:
            self.assertRegex(atom, r"^[<>=~!]*[a-z0-9][a-z0-9+._-]*/[A-Za-z0-9+._-]+$",
                             f"{atom!r} is not an atom portage can parse")
            if atom[0].isalpha():
                # A BARE atom must not look versioned: portage splits on the last hyphen and reads
                # a leading digit as a version, which is exactly how webkit-gtk-6 got through review.
                tail = atom.rsplit("-", 1)[-1]
                self.assertFalse(tail[:1].isdigit(),
                                 f"{atom!r} reads as a versioned atom with no operator — portage "
                                 f"rejects the line and it masks nothing")
        self.assertIn("net-libs/webkit-gtk", atoms,
                      "nothing stops webkit being built from source on this profile")

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

    def test_emerge_sync_works_off_this_lan_on_EVERY_profile(self):
        """rsync://gentoo-repo.lan resolves on exactly one network. Anywhere else that is a broken
        --sync from first boot, and the way somebody finds out is that their machine can never
        update — no error at install time, nothing in any log.

        EVERY arm, not just PosterChanOS. The OS arm was moved to webrsync first and the plain
        server arm was left on the .lan name, on the reasoning that a server profile is "somebody's
        own machines on their own network". That is wrong the moment anybody else installs a server,
        which is the entire point of shipping an installer, and both arms fail identically and
        silently. So this asserts on the WHOLE function: no .lan may appear in the repo config at
        all."""
        repo = self._fn("gentooRepo")
        cfg = repo[repo.index("[gentoo]"):]
        self.assertIn("sync-type = webrsync", cfg)
        self.assertIn("https://gentoo.poster.place", cfg)
        self.assertIn("sync-webrsync-verify-signature = true", cfg,
                      "a package tree fetched over HTTP from somebody's server, unverified")
        # The comment above it is allowed to say the words; a written CONFIG LINE is not.
        for line in repo.splitlines():
            bare = line.strip()
            if bare.startswith("#") or not bare:
                continue
            self.assertNotIn(".lan", bare,
                             "the installer still writes a LAN-only host into portage's config: "
                             + bare.strip())

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
        picks = [ln for ln in self.src.splitlines() if "GENTOO_PROFILE=$(" in ln]
        self.assertTrue(picks, "the profile is no longer chosen here — re-read this test")
        for ln in picks:
            with self.subTest(line=ln.strip()[:60]):
                self.assertIn("-vi 'plasma", ln,
                              "this profile line does not exclude plasma, so KDE arrives through "
                              "the USE flags whatever the package list says")

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
        self.assertNotIn("games-util/steam-launcher", steam,
                         "the native 32-bit Steam path came back — ABI_X86=32 through the whole "
                         "graphics stack, built twice, for a program that ships its own runtime")

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

    def test_there_is_only_one_profile(self):
        """"that file should be focused on PosterChanOS only, we don't need fragmentation."

        It used to build two, chosen by a variable EIGHT places branched on — and every one of those
        was a chance for the halves to disagree. They did: a chroot inherits no environment, so the
        choice had to become a file after a chroot run silently rebuilt the KDE list and installed a
        whole second desktop on the profile whose point is not having one."""
        self.assertNotIn("DESKTOP_APPS", self.src, "the KDE desktop list came back")
        self.assertNotIn('POSTERCHANOS="${POSTERCHANOS:-n}"', self.src,
                         "the profile switch came back — there is nothing on the other side of it")
        self.assertNotIn('"$POSTERCHANOS" = *y*', self.src, "a branch on the removed profile survives")
        self.assertIn('PACKAGES="$BASE_PACKAGES $POSTERCHANOS_PACKAGES"', self.src)

    def test_the_machine_still_says_what_it_is(self):
        """`/etc/posterchanos` outlives the branching it used to drive: nothing reads it to decide
        any more, but an installed system should still be able to identify itself."""
        self.assertIn("touch $TARGET/etc/posterchanos", self.src)

    def test_no_kde_package_is_installed_anywhere(self):
        code = "\n".join(l for l in self.src.splitlines() if not l.lstrip().startswith("#"))
        for pkg in ("kde-plasma/plasma-meta", "kde-apps/dolphin", "kde-apps/konsole",
                    "kde-apps/kdenlive", "kde-plasma/discover"):
            with self.subTest(pkg=pkg):
                self.assertNotIn(pkg, code)


if __name__ == "__main__":
    unittest.main()


class TheMachineCallsItselfPosterChanOS(unittest.TestCase):
    """An operating system that answers "Gentoo" to everything that asks is not branded, however
    good the shell looks.

    The branding was already correct in every string a person reads INSIDE the shell — the plymouth
    theme, the installer's own output, the prose — which is exactly why this gap was easy to miss:
    it is only visible from outside it. The login banner, `hostnamectl`, neofetch, the bootloader
    entry and every crash report read `/etc/os-release`, and nothing wrote one.
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "os", "gentoo.sh"), encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_it_writes_an_os_release(self):
        self.assertIn("/etc/os-release", self.src,
                      "nothing writes an os-release, so the machine answers 'Gentoo'")

    def test_the_display_names_are_capitalised_the_way_the_product_is(self):
        for field in ('NAME="PosterChanOS"', 'PRETTY_NAME="PosterChanOS"'):
            with self.subTest(field=field):
                self.assertIn(field, self.src)

    def test_the_id_is_lowercase_because_the_spec_says_so(self):
        """os-release IDs are lowercase with no spaces. A display string in `ID` breaks the tools
        that key on it, which is the opposite of what branding it was for."""
        self.assertIn("ID=posterchanos", self.src)
        self.assertNotIn("ID=PosterChanOS", self.src)

    def test_it_still_says_it_is_a_gentoo(self):
        """`ID_LIKE` is how portage tooling, bug reporters and anything else reading os-release keep
        treating this as the Gentoo it actually is. Dropping it renames the system to something no
        tool has heard of."""
        self.assertIn("ID_LIKE=gentoo", self.src)

    def test_the_machine_identifiers_are_left_alone(self):
        """These are NOT branding and must never be recapitalised: the chroot marker, the plymouth
        theme directory, the portage mask filename and the package atom. The atom in particular has
        to match the overlay path `os/overlay/app-misc/posterchanos-shell/`, and a capital letter
        there is an install that fails at the last step."""
        for ident in ("/etc/posterchanos", "app-misc/posterchanos-shell",
                      "package.mask/posterchanos"):
            with self.subTest(ident=ident):
                self.assertIn(ident, self.src)


class MoreThanOneScreenWorksWithoutConfiguring(unittest.TestCase):
    """A PLUGGED-IN MONITOR MUST BE REACHABLE, not merely lit.

    sway arranges extra outputs itself and `output *` gives each a background, so a second screen
    comes on by itself. But this session ships no window-management bindings -- every app is a
    floating window opened from the desktop -- so the second screen was a lit panel you could not
    focus, could not move a window onto, and could not launch anything from.

    CHECKED IN BOTH COPIES OF THE CONFIG. The installer writes one and the shell package ships the
    other, and they had already drifted: gentoo.sh still bound $mod+Return to `foot` long after the
    package's copy raised PosterChan's own terminal, so a machine installed from the ISO behaved
    differently from one updated through the package -- reported as "win + enter not loading
    PosterChan terminal", on an install where that had been fixed and shipped to the other file.
    """

    CONFIGS = {}

    @classmethod
    def setUpClass(cls):
        cls.CONFIGS = {"os/gentoo.sh": open(SH, encoding="utf-8").read()}
        overlay = os.path.join(ROOT, "os", "overlay", "app-misc", "posterchanos-shell",
                               "files", "sway.config")
        if os.path.exists(overlay):
            cls.CONFIGS["overlay sway.config"] = open(overlay, encoding="utf-8").read()

    def test_focus_can_reach_another_screen(self):
        for name, cfg in self.CONFIGS.items():
            for d in ("left", "right", "up", "down"):
                self.assertIn(f"focus output {d}", cfg, f"{name}: no way to focus the screen {d}")

    def test_a_window_can_be_moved_to_another_screen(self):
        for name, cfg in self.CONFIGS.items():
            for d in ("left", "right"):
                self.assertIn(f"move container to output {d}", cfg,
                              f"{name}: no way to move a window {d}")

    def test_the_focus_follows_the_window_it_moved(self):
        # Focus left behind reads as having closed the window.
        for name, cfg in self.CONFIGS.items():
            for line in cfg.splitlines():
                if "move container to output" in line:
                    self.assertIn("focus output", line,
                                  f"{name}: moving a window leaves the focus behind: {line.strip()}")

    def test_no_binding_names_a_specific_output(self):
        # HDMI-A-1 is dead on a machine that does not have one.
        for name, cfg in self.CONFIGS.items():
            for line in cfg.splitlines():
                if line.strip().startswith("bindsym") and "output" in line:
                    for dead in ("HDMI", "DP-", "eDP", "VGA"):
                        self.assertNotIn(dead, line, f"{name}: names a specific output: {line.strip()}")

    def test_a_window_can_be_closed(self):
        for name, cfg in self.CONFIGS.items():
            self.assertIn("$mod+q kill", cfg, f"{name}: there is no way to close a window")

    def test_the_terminal_binding_has_not_drifted_again(self):
        """The two files must agree about what Super+Enter does."""
        for name, cfg in self.CONFIGS.items():
            self.assertIn("bindsym $mod+Return exec swaymsg -t send_tick pc:terminal", cfg,
                          f"{name}: Super+Enter does not raise PosterChan's terminal")
            self.assertIn("bindsym $mod+Shift+Return exec foot", cfg,
                          f"{name}: Super+Shift+Enter does not open a plain terminal")


class TorIsUpFromTheFirstBoot(unittest.TestCase):
    """A SYSTEM DAEMON, not the desktop app's bundled one.

    The app's own tor is per-app and dies with the app. This is a SOCKS port every program on the
    machine can use, up before anybody logs in.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = open(SH, encoding="utf-8").read()

    def test_the_package_is_installed(self):
        self.assertIn("net-vpn/tor", self.src, "tor is not in the package list")

    def test_it_is_enabled_at_boot(self):
        self.assertIn("systemctl enable tor", self.src, "tor is installed but never enabled")

    def test_the_country_is_pinned_at_both_ends(self):
        self.assertIn("EntryNodes {us}", self.src)
        self.assertIn("ExitNodes {us}", self.src)

    def test_the_geoip_file_is_configured(self):
        """WITHOUT IT THE COUNTRY LINES DO NOTHING, and nothing says so: tor cannot resolve a {cc}
        code with no GeoIP database, bootstraps to 100%, reports itself healthy and ignores the
        restriction. A config that appears to work and does not is worse than one that refuses to
        start, because the entire point of naming a country is that the traffic goes there."""
        self.assertIn("GeoIPFile", self.src, "ExitNodes {us} cannot be resolved without GeoIPFile")
        self.assertIn("GeoIPv6File", self.src)

    def test_strictnodes_accompanies_the_country(self):
        """It turns the preference into a requirement, so tor fails to build a circuit rather than
        quietly leaving the country when it cannot stay."""
        self.assertIn("StrictNodes 1", self.src)

    def test_the_config_is_rewritten_not_appended(self):
        """Two ExitNodes lines and tor takes the LAST one, so the visible first line is a lie. A
        re-run of the installer must not be able to produce that."""
        i = self.src.index("EntryNodes {us}")
        before = self.src[max(0, i - 1200):i]
        self.assertIn("cat >/etc/tor/torrc", before,
                      "the torrc is not written whole, so re-running could append a second copy")
        self.assertNotIn(">>/etc/tor/torrc", self.src)


class TheDisplayTurnsItselfOff(unittest.TestCase):
    """TWO MINUTES BY DEFAULT, AND CHANGEABLE. A laptop whose screen never blanks is a laptop that
    is warm and flat in the morning, and a timeout compiled into sway's config is one nobody can
    change: that file belongs to portage and etc-update replaces it on upgrade."""

    @classmethod
    def setUpClass(cls):
        cls.src = open(SH, encoding="utf-8").read()
        cls.idle = open(os.path.join(ROOT, "os", "bin", "pc-idle"), encoding="utf-8").read()

    def test_the_package_is_installed(self):
        self.assertIn("gui-apps/swayidle", self.src,
                      "the session runs swayidle but never installs it")

    def test_the_session_starts_it(self):
        self.assertIn("/usr/local/bin/pc-idle", self.src)

    def test_it_ships_with_the_other_helpers(self):
        i = self.src.index("for helper in")
        self.assertIn("pc-idle", self.src[i:i + 200],
                      "pc-idle is started but never copied onto the machine")

    def test_two_minutes_is_the_default(self):
        self.assertIn("DEFAULT=120", self.idle)

    def test_the_timeout_is_read_from_a_file_not_baked_in(self):
        self.assertIn("PC_IDLE_CONF", self.idle)
        self.assertIn("set)", self.idle, "there is no way to change it")

    def test_never_is_an_answer(self):
        """Somebody watching a film should be able to say never, and it must leave no daemon
        running that would blank the screen anyway."""
        self.assertIn('[ "$SECS" -gt 0 ] || exit 0', self.idle)

    def test_only_one_watcher_runs(self):
        """`exec_always` re-runs this on every sway reload, and two swayidles fight over the same
        screen -- one turning it off while the other turns it on."""
        self.assertIn("pkill -x swayidle", self.idle)

    def test_it_does_not_claim_a_dim_it_cannot_do(self):
        """Dimming needs a backlight control, and the profile deliberately carries none
        (brightnessctl is not in the Gentoo tree). A stage that calls something absent is a stage
        that silently does nothing."""
        self.assertNotIn("dpms on", self.idle)
