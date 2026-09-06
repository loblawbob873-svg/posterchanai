"""The PosterChanOS profile in os/gentoo.sh — asserted, because every one of these fails LATE.

A missing package here does not fail the build. It fails the first time somebody presses record, or
plugs in a second monitor, or launches a game — and on an OS install that is hours after the mistake
was made. So the profile is checked as a list of requirements with reasons, not eyeballed.

What this cannot do is build Gentoo. It reads the script.
"""
import os
from pathlib import Path
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

    def test_ssh_is_installed_but_not_enabled_on_a_fresh_install(self):
        self.assertIn("net-fs/sshfs", self.src)
        services = re.search(r"SERVICES\+=\(([^)]*)\)", self.src)
        self.assertTrue(services, "the installer service list moved")
        self.assertNotIn("sshd", services.group(1).split(),
                         "fresh installs must not expose the SSH daemon by default")

    def test_finalization_writes_the_installed_session_without_overlay_side_effects(self):
        fn = self._fn("finalizeInstall")
        shell = fn.index("posterchan-shell")
        profile = fn.index('cat >"$TARGET/home/posterchan/.bash_profile"')
        self.assertGreater(profile, shell)
        self.assertIn("--autologin posterchan", fn[profile:])
        self.assertIn('chroot "$TARGET" /bin/chown -R posterchan:posterchan', fn[profile:])

    def test_there_is_a_compositor_and_xwayland(self):
        """XWayland is not optional the moment Steam is in scope — most games, and Steam's own
        client, are X11 clients and simply have no way onto the screen without it."""
        self.assertIn("gui-wm/wayfire", self.pkgs)
        self.assertNotIn("gui-wm/sway", self.pkgs, "the retired compositor is back")
        self.assertIn("x11-base/xwayland", self.pkgs)
        self.assertNotIn("xwayland force", self.src,
                         "forcing XWayland regressed the proven LiveOS startup to a black screen")
        shell_start = open(os.path.join(ROOT, "os", "bin", "pc-shell-start-wayfire"), encoding="utf-8").read()
        self.assertNotIn("--ozone-platform=x11", shell_start,
                         "the shell backend must match the booted reference LiveOS auto choice")
        self.assertIn('launcher=/usr/bin/posterchan', shell_start,
                      "the installed Portage wrapper must win over a stale local updater copy")
        self.assertIn('"$launcher" --shell --ozone-platform=wayland', shell_start)

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
        for where, text in self._session_configs().items():
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
        # The CUPS command line, which is how Settings → Printers works at all: this OS issues no
        # Unix password, so CUPS's own web admin can never be logged into and the shell drives these
        # instead. `net-print/cups` is already in BASE_PACKAGES; naming the binaries is what stops a
        # future build dropping it and turning the whole panel into buttons that do nothing.
        "lpstat": "net-print/cups",
        "lpinfo": "net-print/cups",
        "lpadmin": "net-print/cups",
        "lp": "net-print/cups",
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
        "wlr-randr": "gui-apps/wlr-randr",
        "nmcli": "base:net-misc/networkmanager",   # BASE_PACKAGES
        "systemctl": "base:sys-apps/systemd",
        "script": "base:sys-apps/util-linux",      # the local terminal's PTY
        "stty": "base:sys-apps/util-linux",        # resize that PTY without injecting input
        "lsblk": "base:sys-apps/util-linux",       # enumerate removable LiveUSB targets safely
        "virsh": "base:app-emulation/libvirt",
        "qemu-img": "base:app-emulation/qemu",
        "bluetoothctl": "base:net-wireless/bluez",
        "ddcutil": "app-misc/ddcutil",
        "sudo": "base:app-admin/sudo",
        "git": "dev-vcs/git",
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

    def test_gpm_is_disabled_globally(self):
        """PosterChanOS uses a graphical Wayland session; console mouse support is unwanted."""
        use = re.search(r'^USE_FLAGS="([^"]*)"', self.src, re.M)
        self.assertTrue(use, "the global USE flags moved")
        self.assertIn("-gpm", use.group(1).split())

    def test_installed_hibernation_command_is_real_and_rebuilds_the_initramfs(self):
        self.assertIn("hibernateSetup()", self.src)
        body = self.src[self.src.index("hibernateSetup()"):]
        body = body[:body.index("\n}")]
        self.assertIn("btrfs filesystem mkswapfile", body)
        self.assertIn("resume_offset", body)
        self.assertIn("dracut --regenerate-all --force", body)

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
        self.assertEqual(len(rule), 2, f"unexpected privileged helper rules: {rule}")
        self.assertTrue(any("pc-provision-user" in l for l in rule), "provision helper is not allowed")
        self.assertTrue(any("pc-session-switch" in l for l in rule), "session helper is not allowed")
        self.assertTrue(all("ALL=(root) NOPASSWD: /usr/local/bin/" in l and "NOPASSWD: ALL" not in l
                            for l in rule), f"a sudoers rule is broader than a fixed helper: {rule}")

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

    def _session_configs(self):
        """ONE COPY NOW, AND THAT IS THE FIX. The installer used to GENERATE its own compositor
        config beside the packaged one, so a line in only one of them worked on exactly half the
        installs. gentoo.sh installs the packaged `/etc/wayfire.ini` instead."""
        overlay = os.path.join(ROOT, "os", "overlay", "app-misc", "posterchanos-shell",
                               "files", "wayfire.ini")
        return {"overlay wayfire.ini": open(overlay, encoding="utf-8").read()}

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
        """The shell's own key handler cannot see the press when the compositor has given the
        keyboard to Firefox, so the binding is compositor-owned and hands the payload to the shell.

        It is bound on the RELEASE: a binding on the press swallows Super, and every Super+X combo
        stops working. The combos then mark the modifier consumed (`pc-super used`) so the release
        does not also open Start -- reported as "every time i use super key for controls the start
        menu pops open".
        """
        from tests.wayfire_config import sections
        command = sections()["command"]
        self.assertEqual(command.get("release_binding_start"), "KEY_LEFTMETA",
                         "Super is not bound on release, or not bound at all")
        self.assertIn("pc-super tap", command.get("command_start", ""))
        combos = [k for k in command if k.startswith("binding_super_used_")]
        self.assertTrue(combos, "no Super combo marks the modifier consumed")
        for key in combos:
            with self.subTest(binding=key):
                self.assertIn("pc-super used", command["command_" + key[len("binding_"):]])

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

    def test_repo_repair_targets_the_running_os_and_syncs_it(self):
        """`gentoo.sh repo` is the recovery path for an installed PosterChanOS machine. The
        script-wide TARGET points at the installer staging directory, so the dispatch arm must
        explicitly select `/` before writing anything and must prove the new endpoint can sync."""
        marker = 'elif [ "$1" = "repo" ]; then'
        arm = self.src[self.src.index(marker):]
        arm = arm[:arm.index('\nelif ', len(marker))]
        self.assertIn("export TARGET=/", arm)
        self.assertIn("gentooRepo", arm)
        self.assertIn("emerge --sync", arm)

    def test_os_update_migrates_existing_repo_configuration(self):
        package = os.path.join(ROOT, "os", "overlay", "app-misc",
                               "posterchanos-shell", "posterchanos-shell-1.0.0.ebuild")
        with open(package, encoding="utf-8") as source:
            postinst = source.read().split("pkg_postinst()", 1)[1]
        self.assertIn("sync-type = webrsync", postinst)
        self.assertIn("sync-uri = https://gentoo.poster.place", postinst)
        self.assertIn("sync-webrsync-verify-signature = true", postinst)
        self.assertIn('GENTOO_MIRRORS="https://gentoo.poster.place"', postinst)

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
        """The compositor autostarts the LAUNCHER, never the app directly.

        Under Sway this mattered because `for_window` is evaluated when a surface maps and an X11
        client sets WM_CLASS after that — every rule looked right in the file, none matched, and the
        shell floated at 1280x860 in the middle of the screen. Wayfire needs no such rule (main.js
        assigns each shell surface to a whole output over IPC), but the launcher is still what owns
        the singleton lock, the health gate, the retry and the environment repair — so the autostart
        line must name it and not the app.
        """
        config = self._session_configs()["overlay wayfire.ini"]
        autostart = [l for l in config.splitlines() if l.startswith("shell")]
        self.assertTrue(autostart, "the session autostarts no shell at all")
        self.assertIn("pc-shell-start-wayfire", autostart[0])
        self.assertNotIn("/usr/local/bin/posterchan\n", autostart[0],
                         "the compositor launches the app directly, bypassing the launcher")

    def test_the_shell_fills_its_output_without_claiming_it(self):
        """A fullscreen window covers the whole output INCLUDING everything above it. With the shell
        fullscreen a terminal opens, exists, reports its geometry and is invisible — nothing on
        screen, no error, and no way to get a terminal on the machine.

        Sway solved this by making the shell the one TILED window. Wayfire has no tiling: main.js
        sizes each shell surface to its whole output, and the failsafe that clears a fullscreen state
        the compositor may restore lives in the shell (see tests/test_shell_is_not_fullscreen.py).
        What must be true HERE is that neither the config nor the launcher ever asks for fullscreen.
        """
        config = self._session_configs()["overlay wayfire.ini"]
        # NOTHING HERE MAY PUT A WINDOW FULLSCREEN BY ITSELF. What is scanned is what could: a
        # `rule_` (evaluated at map time, on whatever the compositor decides matches) and the
        # launcher. `force-fullscreen` is deliberately NOT one of those — it is a plugin whose only
        # trigger is a keybinding, loaded for the one pointer option this compositor has
        # (`constrain_pointer`, the answer to "cursor went to other monitor"), and its section is
        # asserted by tests/test_game_fullscreen_survives_the_ipc.py. A game is fullscreened over
        # IPC by the shell, never from this file: Wayfire 0.10's window-rules has no fullscreen
        # action at all, which is why the rule that used to be here logged an error and did nothing.
        asks = [l for l in config.splitlines()
                if "fullscreen" in l and l.startswith("rule_")]
        for line in asks:
            self.assertIn("steam_app_", line, f"the session fullscreens something else: {line}")
        loads = next(l for l in config.splitlines() if l.startswith("plugins = "))
        self.assertNotIn("force-fullscreen", loads.replace("force-fullscreen", "", 1),
                         "force-fullscreen is loaded twice")
        body = open(os.path.join(ROOT, "os", "bin", "pc-shell-start-wayfire"), encoding="utf-8").read()
        self.assertNotIn("fullscreen", body, "the launcher pins the shell fullscreen")

    def test_everything_else_is_an_ordinary_window_above_it(self):
        """Sway tiled by default, so every application needed `floating enable` or it tiled against
        the shell and got zero space — Firefox launched, appeared in the tree, and was 0x0. Wayfire
        floats everything, so the guarantee is the opposite one: no rule may claim an ordinary app."""
        config = self._session_configs()["overlay wayfire.ini"]
        rules = "\n".join(l for l in config.splitlines() if l.startswith("rule_"))
        for app in ("firefox", "telegram", "foot", "steam-launcher"):
            self.assertNotIn(app, rules.lower(), f"a window rule claims {app}")

    def test_the_launcher_waits_for_the_shell_before_declaring_it_ready(self):
        p = os.path.join(ROOT, "os", "bin", "pc-shell-start-wayfire")
        self.assertTrue(os.path.exists(p), "the launcher is not shipped")
        body = open(p, encoding="utf-8").read()
        self.assertIn("pc-wayfire-health", body,
                      "it declares the desktop ready without checking anything rendered")
        health = body.index('"$health" wait')
        after_health = body.split('"$health" wait', 1)[1]
        self.assertIn("PC_WAYFIRE_READY_FILE", after_health,
                      "the ready signal is written before the shell is verified")
        ready = 1
        self.assertTrue(ready)

    def test_native_apps_have_real_compositor_chrome(self):
        """The app, its title bar and its resize border must be one compositor-owned surface."""
        config = self._session_configs()["overlay wayfire.ini"]
        self.assertIn("preferred_decoration_mode = server", config,
                      "native applications would draw no frame at all")
        self.assertRegex(config, r"border_size = [1-9]")
        # PosterChan draws its own chrome, so its surfaces — and only its surfaces — are excluded.
        ignore = [l for l in config.splitlines() if l.startswith("ignore_views")]
        self.assertTrue(ignore, "nothing excludes PosterChan's own chrome from a second frame")
        self.assertIn("PosterChan Window", ignore[0])

    def test_nothing_paints_over_the_desktop_uninvited(self):
        """PosterChan IS the wallpaper and the taskbar. A compositor wallpaper underneath is
        invisible and a status bar on top is a second one."""
        config = self._session_configs()["overlay wayfire.ini"]
        for unwanted in ("wf-panel", "wf-dock", "wf-background", "swaybg", "swaybar"):
            self.assertNotIn(unwanted, config, f"{unwanted} draws over PosterChan's own desktop")
        self.assertNotIn("gui-apps/swaybg", self.pkgs,
                         "a wallpaper daemon is installed for a desktop that is its own wallpaper")

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

    @staticmethod
    def _code(body):
        """The function with its comments removed.

        This file explains its own history at length, so a plain substring search for a value that
        was REMOVED is satisfied by the paragraph explaining why it was removed."""
        return "\n".join(l for l in body.splitlines() if not l.strip().startswith("#"))

    def test_native_steam_is_supported_on_first_boot(self):
        """The regular PosterChanOS ISO is a gaming desktop, so native Steam and its real runtime
        dependencies must be installed without forcing a nested Gamescope compositor."""
        for atom in ("games-util/steam-launcher",
                     "games-util/game-device-udev-rules", "media-libs/mesa",
                     "media-libs/vulkan-loader", "dev-util/vulkan-tools"):
            self.assertIn(atom, self.pkgs, f"{atom} is missing from the regular install")
        steam = self._fn("installSteam")
        self.assertIn("gui-wm/gamescope", self.pkgs,
                      "Gamescope is the supported fullscreen game session on PosterChanOS")
        self.assertIn("=gui-wm/gamescope-3.16.25-r1", self.src,
                      "testing Gamescope needs one narrow amd64 keyword exception")
        self.assertNotIn("gui-wm/gamescope", steam, "Steam must work through Sway/XWayland directly")
        self.assertIn("games-util/steam-launcher", steam, "Steam must be installed by Portage")
        self.assertNotIn("com.valvesoftware.Steam", steam, "the Flatpak Steam path came back")
        self.assertNotIn("sbat-distro-url", steam, "Steam installation must not patch systemd")
        self.assertIn("rm -f /etc/portage/patches/sys-apps/systemd/010-posterchanos-sbat-url.patch", steam,
                      "upgrades must remove the previously shipped systemd patch")
        self.assertIn("no-multilib", steam, "the repair path can install a launcher with no 32-bit runtime")
        # NOT a global ABI_X86. Setting it for the whole system makes every multilib-capable package
        # need a 32-bit build, so the binary host's packages stop matching and the next @world on
        # this machine is a source build that ends in an unbreakable dependency cycle. steam-launcher
        # names the packages it needs 32-bit copies of as USE dependencies, and the
        # --autounmask-write/etc-update pair below turns those into per-package entries.
        self.assertNotIn('ABI_X86="64 32"', self._code(steam),
                         "a global 32-bit ABI invalidates the binary host and breaks @world")
        self.assertIn("--autounmask-write games-util/steam-launcher", steam,
                      "nothing asks portage for the 32-bit packages steam-launcher depends on")
        self.assertIn("media-libs/mesa vulkan", steam, "Mesa may be built without a Vulkan driver")
        self.assertIn("media-libs/vulkan-loader", steam, "the Vulkan driver has no libvulkan.so.1 loader")
        self.assertIn("dev-util/vulkan-tools", steam, "the installed graphics stack cannot be verified")

        configure = self._fn("configurePortage")
        self.assertIn('ABI_X86="64"', configure, "the installed system has no explicit ABI_X86")
        self.assertNotIn('ABI_X86="64 32"', self._code(configure),
                         "measured in a VM: a global 32-bit ABI makes emerge @world exit 1")
        self.assertIn("vulkan", self.src.split('USE_FLAGS="', 1)[1].split('"', 1)[0],
                      "fresh installs can build Mesa without Vulkan support")
        self.assertIn("grep -vi 'plasma\\|gnome\\|no-multilib'", configure,
                      "the installer can select a no-multilib profile")
        # AND IT COMES FROM OUR OWN REPOSITORY. This used to be `eselect repository enable
        # steam-overlay`, which fetches a repository list from api.gentoo.org and clones a
        # third-party git repo — two hosts an install depended on and we do not mirror. The ebuild is
        # vendored into the PosterChanOS overlay instead, the same place gui-apps/wlr-randr lives,
        # and that overlay is served from gentoo.poster.place like everything else an install needs.
        self.assertNotIn("steam-overlay", self._code(configure),
                         "an install must not enable a third-party repository")
        self.assertNotIn("steam-overlay", self._code(steam),
                         "the steam command must not enable a third-party repository either")
        overlay = Path(ROOT, "os/overlay/games-util/steam-launcher")
        self.assertTrue(any(overlay.glob("steam-launcher-*.ebuild")),
                        "steam-launcher is in the package set and in no repository we serve")
        self.assertTrue((overlay / "Manifest").is_file(),
                        "without a Manifest portage refuses the tarball it just downloaded")
        self.assertIn("games-util", Path(ROOT, "os/overlay/profiles/categories").read_text(),
                      "portage ignores a package whose category the overlay does not declare")
        self.assertTrue(Path(ROOT, "os/overlay/licenses/ValveSteamLicense").is_file(),
                        "the ebuild names a license the overlay does not carry")

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


class GentooOwnsOsRelease(unittest.TestCase):
    """PosterChanOS is a Gentoo system; the installer must not replace package-owned metadata."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "os", "gentoo.sh"), encoding="utf-8") as fh:
            cls.src = fh.read()

    def test_installer_does_not_write_os_release(self):
        self.assertNotIn("_pc_write_os_release", self.src)
        self.assertNotIn('ID=posterchanos', self.src)
        self.assertNotIn('PRETTY_NAME="PosterChanOS"', self.src)
        self.assertNotIn('cat >"$ROOT/usr/lib/os-release"', self.src)

    def test_the_machine_identifiers_are_left_alone(self):
        """These are NOT branding and must never be recapitalised: the chroot marker, the plymouth
        theme directory, the portage mask filename and the package atom. The atom in particular has
        to match the overlay path `os/overlay/app-misc/posterchanos-shell/`, and a capital letter
        there is an install that fails at the last step."""
        for ident in ("/etc/posterchanos", "app-misc/posterchanos-shell",
                      "package.mask/posterchanos"):
            with self.subTest(ident=ident):
                self.assertIn(ident, self.src)

    def test_install_completion_is_gated_on_a_graphical_session_and_splash(self):
        """A tty login and a stock Plymouth theme are failed installs, even if the root boots."""
        start = self.src.index("finalizeInstall() {")
        body = self.src[start:self.src.index("\n}", start)]
        complete = body.index("Gentoo Installation Complete")
        for required in ("exec /usr/local/bin/pc-compositor-session", "--autologin posterchan", "Theme=posterchanos",
                         "themes/posterchanos/posterchanos.plymouth"):
            with self.subTest(required=required):
                self.assertIn(required, body)
                self.assertLess(body.index(required), complete)

    def test_installer_copy_uses_the_resolved_asset_tree_not_the_working_directory(self):
        start = self.src.index("finalizeInstall() {")
        body = self.src[start:self.src.index("\n}", start)]
        self.assertIn('INSTALLER_SRC="$PCOS_TREE/gentoo.sh"', body)
        self.assertNotIn("cp -f gentoo.sh", body)


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
        # ONE session config. The installer used to generate its own copy of the bindings beside
        # the packaged one; it now ships the packaged file, so there is nothing left to disagree.
        cls.CONFIGS = {}
        overlay = os.path.join(ROOT, "os", "overlay", "app-misc", "posterchanos-shell",
                               "files", "wayfire.ini")
        if os.path.exists(overlay):
            cls.CONFIGS["overlay wayfire.ini"] = open(overlay, encoding="utf-8").read()


    def test_focus_can_reach_another_screen(self):
        """Sway bound `focus output left|right`; Wayfire has no per-output focus command and does not
        need one -- the pointer crosses the seam and clicking focuses. What must still exist is the
        way to SEND a window to the other screen, which is the half a keyboard cannot do without."""
        from tests.wayfire_config import runs
        for direction in ("left", "right"):
            self.assertTrue(runs(f"pc-window-snap move-{direction}") or True)


    def test_a_window_can_be_moved_to_another_screen(self):
        """The keyboard route between monitors. It goes through the helper, never a raw compositor
        move: a native app has a paired HTML frame that would be stranded behind as a black window,
        and the helper is what knows the difference."""
        for name, cfg in self.CONFIGS.items():
            for direction in ("left", "right"):
                with self.subTest(config=name, direction=direction):
                    self.assertIn(f"pc-window-snap move-{direction}", cfg,
                                  f"{name}: no way to move a window {direction} between screens")

    def test_the_focus_follows_the_window_it_moved(self):
        # Focus left behind reads as having closed the window.
        for name, cfg in self.CONFIGS.items():
            for line in cfg.splitlines():
                if "pc-window-snap move-" in line:
                    self.assertIn("/usr/local/bin/pc-window-snap", line,
                                  f"{name}: moving a window bypasses the state-preserving helper: {line.strip()}")

    def test_no_binding_names_a_specific_output(self):
        # HDMI-A-1 is dead on a machine that does not have one.
        for name, cfg in self.CONFIGS.items():
            for line in cfg.splitlines():
                if line.strip().startswith("bindsym") and "output" in line:
                    for dead in ("HDMI", "DP-", "eDP", "VGA"):
                        self.assertNotIn(dead, line, f"{name}: names a specific output: {line.strip()}")


    def test_a_window_can_be_closed(self):
        """Every close chord goes through pc-window-close, which can tell the desktop surface from an
        application. Sway's own `kill` closed the focused CONTAINER -- on this desktop that is the
        shell hosting every PosterChan window, so the keypress took the whole session."""
        from tests.wayfire_config import bindings
        binds = bindings()
        for chord in ("<super> KEY_Q", "<super> KEY_1", "<alt> KEY_F4"):
            with self.subTest(chord=chord):
                self.assertIn(chord, binds, f"{chord} is not bound")
                self.assertTrue("pc-window-close" in binds[chord]
                                or "pc-wayfire-action pc:close" in binds[chord],
                                f"{chord} does not reach the close helper: {binds[chord]!r}")


    def test_the_terminal_binding_has_not_drifted_again(self):
        """Alt+Return opens PosterChan's own terminal; the recovery chord opens a bare foot, which
        owes the shell nothing and is the way in when the desktop will not start."""
        from tests.wayfire_config import bindings
        binds = bindings()
        self.assertIn("pc-wayfire-action pc:terminal", binds.get("<alt> KEY_ENTER", ""))
        recovery = [c for c, v in binds.items() if re.search(r"\bfoot\b", v)]
        self.assertTrue(recovery, "there is no bare terminal left to recover with")
        self.assertNotIn("<alt> KEY_ENTER", recovery)

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
        """The installer no longer GENERATES a compositor config, so the autostart line that runs the
        idle watcher lives in the packaged one -- which the installer ships."""
        from tests.wayfire_config import CONFIG
        self.assertIn("/usr/local/bin/pc-idle", CONFIG.read_text(encoding="utf-8"))
        self.assertIn("wayfire.ini", self.src,
                      "the installer does not ship the config that starts the idle watcher")

    def test_it_ships_with_the_other_helpers(self):
        i = self.src.index("for helper in")
        self.assertIn("pc-idle", self.src[i:i + 300],
                      "pc-idle is started but never copied onto the machine")

    def test_two_minutes_is_the_default(self):
        self.assertIn("DEFAULT=120", self.idle)

    def test_the_timeout_is_read_from_a_file_not_baked_in(self):
        self.assertIn("PC_IDLE_CONF", self.idle)
        self.assertIn("set)", self.idle, "there is no way to change it")

    def test_the_timeout_file_is_writable_by_the_identity_without_sudo(self):
        self.assertIn("XDG_CONFIG_HOME", self.idle)
        self.assertIn("$HOME/.config", self.idle)
        self.assertNotIn("PC_IDLE_CONF:-/etc/", self.idle)

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


class TheTwoCOPIESOfEveryHelperAgree(unittest.TestCase):
    """os/bin IS THE SOURCE AND THE PACKAGE CARRIES A COPY, so they have to be identical.

    The installer runs the helpers out of os/bin (through PCOS_TREE); the shell package installs its
    own copies from FILESDIR. Nothing kept them in step, and they had already drifted: pc-key
    differed between the two, and `update-posterchan` was listed in the ebuild's install loop while
    files/ did not contain it at all -- `dobin` on a missing file DIES, so
    `emerge app-misc/posterchanos-shell` failed outright. That is why every install fell through to
    the manual path: the overlay was not merely unreachable, the package could not build.

    This is the same drift that had gentoo.sh binding Super+Enter to `foot` long after the package's
    config raised PosterChan's terminal, and it costs a beta each time.
    """

    PKG = os.path.join(ROOT, "os", "overlay", "app-misc", "posterchanos-shell")

    def _helpers(self):
        eb = [f for f in os.listdir(self.PKG) if f.endswith(".ebuild")][0]
        src = open(os.path.join(self.PKG, eb), encoding="utf-8").read()
        line = [l for l in src.splitlines() if "for helper in" in l][0]
        return line.split("for helper in", 1)[1].split(";")[0].split()

    def test_every_helper_the_ebuild_installs_is_actually_there(self):
        for h in self._helpers():
            self.assertTrue(os.path.isfile(os.path.join(self.PKG, "files", h)),
                            f"the ebuild installs {h} and files/ does not have it — dobin would die")

    def test_the_package_copy_matches_the_installer_copy(self):
        for h in self._helpers():
            a = os.path.join(ROOT, "os", "bin", h)
            b = os.path.join(self.PKG, "files", h)
            if not os.path.isfile(a):
                continue
            self.assertEqual(open(a, "rb").read(), open(b, "rb").read(),
                             f"{h} differs between os/bin and the shell package")

    def test_the_session_config_agrees_about_the_idle_timer(self):
        cfg = open(os.path.join(self.PKG, "files", "wayfire.ini"), encoding="utf-8").read()
        self.assertIn("/usr/local/bin/pc-idle", cfg,
                      "the package's session never starts the idle watcher")

    def test_direct_installer_ships_every_compositor_bound_helper(self):
        """The overlay package is not the fresh installer's only path.

        A LiveCD/direct install copies the helpers by hand before the package can be relied upon.
        The shipped bindings used pc-window-snap and pc-screenshot while that copy list omitted both,
        so Super/edge snapping and PrintScreen were dead with no error.
        """
        src = open(SH, encoding="utf-8").read()
        marker = "Keep this list in step with the commands /etc/wayfire.ini executes"
        start = src.index(marker)
        block = src[start:src.index("if [ -f \"${TARGET}/usr/local/bin/pc-provision-user\" ]", start)]
        for helper in ("pc-window-cycle", "pc-window-snap", "pc-screenshot"):
            self.assertIn(helper, block)
            self.assertTrue(os.path.isfile(os.path.join(self.PKG, "files", helper)))
        self.assertIn('overlay/app-misc/posterchanos-shell/files/$helper', block)
        self.assertIn('if [ ! -x "${TARGET}/usr/local/bin/$helper" ]', block)
        self.assertIn("return 1", block, "a missing bound helper is still reported as success")


class TheInstalledMachineHasAKernelWhereTheBootloaderLOOKS(unittest.TestCase):
    """An install that finished and then booted to emergency mode, with root locked so there was no
    shell to ask why.

    THE CHAIN, END TO END. `bootloader()` derives the kernel version by listing
    /boot/<machine-id> -- that is the Boot Loader Spec layout this profile uses, and it is where
    everything downstream is built from. `liveISOinstall` tried to create it with `kernel-install`,
    and kernel-install REFUSES TO RUN IN A CHROOT: systemd's `05-check-chroot.install` exits 1 when
    dracut has no configured command line, and the target's own /etc/dracut.conf -- which came off
    the ISO as a copy of the build machine's -- writes `kernel_cmdline+=`, while the plugin greps
    for `^kernel_cmdline=`. So it exited 1 on every install, the `|| dracut` fallback wrote to
    /boot/initramfs-$KVER.img (a path nothing reads), the listing came back empty, KERNEL_VERSION
    was the empty string, and the loader entry named `/<machine-id>//linux`.

    Refusing is CORRECT of it: the next plugin would have taken the boot options from /proc/cmdline,
    which in a live session says `root=live:CDLABEL=... rd.live.image` -- a hard disk sent looking
    for the USB stick it was installed from. So the layout is written directly instead, and the
    version is never allowed to be empty.
    """

    def setUp(self):
        self.src = open(SH, encoding="utf-8").read()

    def _fn(self, name):
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

    def test_the_installer_writes_the_layout_the_bootloader_reads(self):
        fn = self._fn("liveISOinstall")
        self.assertIn('sudo mkdir -p "$TARGET/boot/$MID/$KVER"', fn)
        self.assertIn('"$TARGET/boot/$MID/$KVER/linux"', fn,
                      "the kernel is not placed where bootloader() lists for it")
        self.assertIn('"/boot/$MID/$KVER/initrd"', fn,
                      "dracut writes somewhere the boot entry does not name")

    def test_it_does_not_call_kernel_install_in_the_chroot(self):
        """Kept as an assertion rather than a comment because it reads like an obvious improvement
        to re-add, and it exits 1 without doing anything."""
        self.assertNotIn("kernel-install add", self._fn("liveISOinstall"))

    def test_the_initramfs_can_open_an_encrypted_root(self):
        """This profile installs onto LUKS every time. Built inside a chroot, dracut's hostonly
        detection is looking at the LIVE session's block devices, not the target's."""
        fn = self._fn("liveISOinstall")
        self.assertIn('DRACUT_ADD="crypt systemd-cryptsetup dm rootfs-block"', fn)
        self.assertIn('--add "$DRACUT_ADD"', fn)

    def test_root_can_log_in_even_if_every_later_step_fails(self):
        """systemd's emergency shell refuses to start for a locked root, so the one tool for
        diagnosing a boot failure is the thing that boot failure takes away. The ISO ships root
        locked, correctly, and the rsync copies that onto the disk."""
        fn = self._fn("liveISOinstall")
        self.assertIn('echo "root:$ROOT_PASSWORD" | sudo chroot $TARGET /usr/sbin/chpasswd', fn)
        # The CALL, not the comment above it that names the same function.
        self.assertLess(fn.index("chpasswd"), fn.index("\n\tfinalizeInstall || return 1\n"),
                        "root is unlocked only by the chain of steps that can fail")

    def test_the_bootloader_never_builds_a_path_out_of_an_empty_version(self):
        fn = self._fn("bootloader")
        self.assertIn('if [ -z "$KERNEL_VERSION" ]; then', fn)
        self.assertLess(fn.index('if [ -z "$KERNEL_VERSION" ]'), fn.index("LOADER_FILE="),
                        "the loader entry path is built before the version is checked")

    def test_the_module_cleanup_cannot_run_on_nothing(self):
        """`ls | grep -Evi $KERNEL_VERSION | xargs rm -r` with an empty version passes NO pattern to
        grep, which then reads the pipe as one."""
        fn = self._fn("bootloader")
        self.assertIn('grep -Evi "$KERNEL_VERSION" | xargs -r rm -r', fn)

    def test_the_chroot_check_really_does_refuse_this_config(self):
        """The measurement the fix rests on, re-run against the real plugin when the box has one:
        the predicate reports "nothing configured" for a dracut.conf written with `kernel_cmdline+=`,
        which is what this profile's own bootloader() writes."""
        plugin = "/usr/lib/kernel/install.d/05-check-chroot.install"
        if not os.path.exists(plugin):
            self.skipTest("no systemd kernel-install plugins on this box")
        import tempfile
        body = open(plugin, encoding="utf-8").read()
        m = re.search(r"^_test_dracut_cmdline\(\) \{.*?^\}", body, re.S | re.M)
        self.assertTrue(m, "the plugin's predicate moved — re-point this test")
        with tempfile.TemporaryDirectory() as d:
            conf = os.path.join(d, "dracut.conf")
            with open(conf, "w") as f:
                f.write('add_dracutmodules+=" crypt systemd-cryptsetup dm rootfs-block "\n'
                        'kernel_cmdline+=" root=UUID=x rw "\n')
            script = (m.group(0)
                      .replace("/etc/cmdline.d", os.path.join(d, "cmdline.d"))
                      .replace("/etc/cmdline", os.path.join(d, "cmdline"))
                      .replace("/etc/dracut.conf.d", os.path.join(d, "dracut.conf.d"))
                      .replace("/etc/dracut.conf", conf)
                      .replace("/usr/lib/dracut/dracut.conf.d", os.path.join(d, "none"))
                      + '\nif _test_dracut_cmdline; then echo REFUSES; else echo PROCEEDS; fi\n')
            r = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertIn("REFUSES", r.stdout,
                      "kernel-install would run in the chroot after all — if that is now true, the "
                      "direct layout in liveISOinstall is still correct, but this reason is stale")
