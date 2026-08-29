"""The live ISO can install itself, and does not carry the machine it was built from.

    "we need to make sure that the livecd is built with gentoo.sh so they can install it on any
     system"
    "laptop needs to reflect a new os install and not have verita84 configured at all"

Two separate problems with one builder.

INSTALLABLE. The builder's own header said "not an installer image" — it made a live disc of a
running machine with no way to adopt it. Worse, the installer IS gentoo.sh, which lives in a home
directory, and `/home` is excluded by default: the one file needed was the one guaranteed to be
missing. It is injected as pseudo-files now, with a .desktop entry so the live session's own start
menu lists it (that menu already reads every .desktop on the machine).

CLEAN. An ISO of your machine is a copy of your machine: your account, your password hash, your ssh
HOST keys, your saved wifi, your history — and it autologins as you on somebody else's hardware. The
ssh host keys are the sharp one, since every machine installed from the ISO would present the same
identity.

The account rewriting is RUN here against fixtures, not grepped, because it is the part that fails
in two opposite and equally silent ways: leaving a real account in (a leak), or dropping root and the
system users (an image that cannot boot).
"""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENTOO = ROOT / "os" / "gentoo.sh"


class TheBuilderShipsTheInstaller(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = GENTOO.read_text()
        i = cls.src.index("liveCD() {")
        cls.fn = cls.src[i:cls.src.index("\n}", i)]

    def test_the_script_directory_goes_into_the_image(self):
        self.assertIn("usr/local/share/posterchanos", self.fn)

    def test_it_takes_the_whole_tree_not_just_the_script(self):
        """gentoo.sh reads `$(dirname $0)/bin` and `/plymouth` and half-works without them."""
        self.assertIn("find \"$IHERE\"", self.fn)

    def test_it_does_not_copy_into_the_running_system(self):
        """Building an ISO must not modify the machine being imaged — the same rule the fstab
        rewrite follows."""
        i = self.fn.index('pseudoput "usr/local/share/posterchanos/$REL"')
        seg = self.fn[max(0, i - 600):i + 600]
        self.assertNotIn("cp -r /usr/local/share", seg)
        self.assertIn("cat ", seg)

    def test_the_installer_is_executable_in_the_image(self):
        i = self.fn.index('"usr/local/share/posterchanos/$REL" f')
        self.assertIn("755", self.fn[i:i + 80])

    def test_there_is_a_way_to_find_it(self):
        """A terminal command nobody is told about is not a way to install an operating system."""
        self.assertIn("posterchanos-install.desktop", self.fn)
        self.assertIn("Install PosterChanOS", self.fn)

    def test_the_desktop_entry_can_reach_root(self):
        i = self.fn.index("[Desktop Entry]")
        entry = self.fn[i:i + 700]
        self.assertIn("sudo", entry)
        self.assertIn("/usr/bin/gentoo.sh", entry)
        self.assertIn("install-live", entry)

    def test_install_retry_cleans_stale_target_mounts(self):
        i = self.src.index("systemMounts() {")
        body = self.src[i:self.src.index("\n}", i)]
        self.assertIn('findmnt -Rrn -o TARGET "$TARGET"', body)
        self.assertIn('mount -t vfat "$EFI" "$TARGET/boot"', body)

    def test_successful_headless_build_returns_success(self):
        done = self.src.index('◆ DONE ◆', self.src.index("liveCD() {"))
        end = self.src.index("\n}\n", done)
        self.assertIn('return 0', self.src[done:end])

    def test_chroot_bootloader_uses_the_chroot_as_target(self):
        branch = self.src[self.src.index('elif [ "$1" = "bootloader" ]'):]
        self.assertLess(branch.index("export TARGET=/"), branch.index("bootloader\n"))

    def test_new_unformatted_esp_is_detected_before_mkfs(self):
        """A new GPT ESP has no FSTYPE until the installer formats it."""
        self.assertIn('FSTYPE,PARTTYPE "$DISK_PATH"', self.src)
        self.assertIn('c12a7328-f81f-11d2-ba4b-00a0c93ec93b', self.src)
        self.assertIn('mkfs.vfat -F 32 "$EFI"', self.src)
        self.assertIn('mountpoint -q "$TARGET/boot"', self.src)

    def test_default_install_disk_rejects_virtual_floppy_and_tiny_devices(self):
        i = self.src.index("setDevices() {")
        body = self.src[i:self.src.index("\n}\n", i)]
        self.assertIn("lsblk -bdnro NAME,TYPE,SIZE", body)
        self.assertIn("^(fd|sr|zram|loop|ram)", body)
        self.assertIn("8589934592", body)

    def test_live_install_prepares_a_fresh_disk_itself(self):
        """The Start-menu launcher enters install-live directly; requiring a separate initialize
        menu first is not a one-click installer and used to copy the image into live /tmp."""
        i = self.src.index("liveISOinstall() {")
        body = self.src[i:self.src.index("\n}", i)]
        self.assertIn("prepareInstallDisk || return 1", body)
        self.assertIn("systemMounts || {", body)
        self.assertIn("Nothing was copied", body)

    def test_disk_preparation_verifies_fat_luks_and_mapper(self):
        i = self.src.index("prepareInstallDisk() {")
        body = self.src[i:self.src.index("\n}", i)]
        for proof in ("wipefs -a", "partprobe", "udevadm settle", "luksFormat --batch-mode",
                      "mkfs.btrfs -f", "mkfs.vfat -F 32", "cryptsetup isLuks"):
            self.assertIn(proof, body)
        self.assertGreaterEqual(body.count("partitionDetection"), 2,
                                "the LUKS UUID is not re-read after formatting")

    def test_live_install_never_uses_the_legacy_shared_password(self):
        i = self.src.index("liveISOinstall() {")
        body = self.src[i:self.src.index("\n}", i)]
        self.assertIn("readInstallPassword confirm || return 1", body)
        self.assertIn("readInstallPassword existing || return 1", body)
        i = self.src.index("readInstallPassword() {")
        password = self.src[i:self.src.index("\n}", i)]
        self.assertIn("read -r -s", password, "the disk password is echoed on screen")
        self.assertIn('DISK_PASSWORD="$first"', password)
        self.assertIn('ROOT_PASSWORD="$first"', password)
        self.assertIn("Passwords did not match", password)
        self.assertNotIn(">/tmp/disk", password)

    def test_selected_luks_password_reaches_target_bootloader_without_persistence(self):
        finalize = self.src[self.src.index("finalizeInstall() {"):
                            self.src.index("\n}\n\ninstallPackages()", self.src.index("finalizeInstall() {"))]
        self.assertIn('PC_INSTALL_PASSWORD="$DISK_PASSWORD" HOME=/root USER=root LOGNAME=root',
                      finalize)
        self.assertIn('chroot "$TARGET" /setup.sh', finalize)
        boot = self.src[self.src.index("bootloader() {"):
                        self.src.index("\n}\n", self.src.index("bootloader() {"))]
        self.assertIn('DISK_PASSWORD="$PC_INSTALL_PASSWORD"', boot)
        setup_lines = self.src[self.src.index("printf '%s\\n' '#!/usr/bin/bash' 'set -e'"):
                               self.src.index("# Do not carry the LiveCD operator")]
        self.assertNotIn("PC_INSTALL_PASSWORD", setup_lines)

    def test_clean_image_gets_a_fresh_fail_fast_finalizer(self):
        """Clean media omits the build host's /setup.sh, so finalization must not rely on rsync
        having copied one.  Otherwise the append operations recreate it without `set -e` and a
        failed bootloader is followed by a false Installation Complete message."""
        finalize = self.src[self.src.index("finalizeInstall() {"):
                            self.src.index("\n}\n\ninstallPackages()", self.src.index("finalizeInstall() {"))]
        create = finalize.index(': >"$TARGET/setup.sh" || return 1')
        fail_fast = finalize.index("printf '%s\\n' '#!/usr/bin/bash' 'set -e'")
        bootloader = finalize.index("gentoo.sh bootloader")
        self.assertLess(create, fail_fast)
        self.assertLess(fail_fast, bootloader)
        self.assertNotIn("sed -i '1i set -e'", finalize)

    def test_installer_staging_directories_are_idempotent(self):
        """The command dispatcher is sourced repeatedly inside the target chroot. Existing
        staging directories are normal and must not emit scary, false `cannot create` errors."""
        preamble = self.src[:self.src.index("######################################",
                                            self.src.index("TARGET='/tmp/install'"))]
        self.assertNotRegex(preamble, r"(?m)^mkdir .*TARGET")
        fstab = self.src[self.src.index("fstab() {"):
                         self.src.index("\n}", self.src.index("fstab() {"))]
        self.assertIn('mkdir -p "$TARGET/etc"', fstab)
        self.assertNotRegex(self.src, r"(?m)^mkdir \\$TARGET$")

    def test_target_finalization_commands_dispatch_at_chroot_root(self):
        """setup.sh runs these modes inside the target. Leaving the historical /tmp/install
        default makes service/profile writes disappear into a nested staging tree."""
        dispatcher = self.src[self.src.index('if [ "$1" = "services" ]'):]
        preamble = self.src[:self.src.index("######################################",
                                            self.src.index("TARGET='/tmp/install'"))]
        for mode in ("services", "accounts", "bootloader", "posterchan-shell"):
            self.assertIn(mode, preamble, f"{mode} is not selected before shared initialization")
            match = re.search(rf'(?:if|elif) \[ "\$1" = "{re.escape(mode)}" \]; then\n'
                              r'(?P<body>.*?)(?=elif \[|else\n|fi\n)', dispatcher, re.S)
            self.assertIsNotNone(match, f"missing {mode} dispatcher")
            snippet = match.group("body")
            self.assertIn("TARGET=/", snippet, f"{mode} does not target the chroot root")

    def test_finalizer_does_not_leak_live_installer_home_into_chroot(self):
        """chroot preserves HOME unless it is replaced.  The live value points at the host-side
        staging tree and caused each target command to emit a `/tmp/install` mkdir diagnostic."""
        finalize = self.src[self.src.index("finalizeInstall() {"):
                            self.src.index("\n}\n\ninstallPackages()", self.src.index("finalizeInstall() {"))]
        self.assertIn('PC_INSTALL_PASSWORD="$DISK_PASSWORD" HOME=/root USER=root LOGNAME=root',
                      finalize)
        self.assertIn('HOME=/root USER=root LOGNAME=root TERM="${TERM:-dumb}"', finalize)
        self.assertRegex(
            finalize,
            r'PC_INSTALL_PASSWORD="\$DISK_PASSWORD" HOME=/root USER=root LOGNAME=root '
            r'TERM="\$\{TERM:-dumb\}" \\\n\s*chroot "\$TARGET" /setup\.sh',
        )
        self.assertRegex(
            finalize,
            r'HOME=/root USER=root LOGNAME=root TERM="\$\{TERM:-dumb\}" \\\n'
            r'\s*chroot "\$TARGET" /usr/bin/bash /usr/bin/gentoo\.sh posterchan-shell',
        )

    def test_installed_disk_pins_and_reads_back_the_getty_boot_target(self):
        """The stage machine's default.target must not decide whether the installed disk reaches tty1."""
        finalize = self.src[self.src.index("finalizeInstall() {"):
                            self.src.index("\n}\n\ninstallPackages()", self.src.index("finalizeInstall() {"))]
        set_default = 'systemctl set-default multi-user.target || return 1'
        get_default = 'systemctl get-default 2>/dev/null)" != multi-user.target'
        self.assertIn(set_default, finalize)
        self.assertIn(get_default, finalize)
        self.assertLess(finalize.index(set_default), finalize.index(get_default))
        self.assertLess(finalize.index(get_default), finalize.index("Gentoo Installation Complete!"))

    def test_completed_live_install_returns_success_without_home(self):
        i = self.src.index("liveISOinstall() {")
        end = self.src.index("\n}\n\nbackupOS()", i)
        tail = self.src[self.src.rindex("finalizeInstall || return 1", i, end):end]
        self.assertIn("return 0", tail)
        self.assertNotRegex(tail, r"(?m)^\s*cd\s*$")


class TheImageDoesNotCarryTheOperator(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = GENTOO.read_text()
        i = src.index("liveCD() {")
        cls.fn = src[i:src.index("\n}", i)]

    def test_it_asks_and_defaults_to_clean(self):
        m = re.search(r"read -p \"Clean out[^\"]*\" -e -i \"([yn])\" CLEAN", self.fn)
        self.assertIsNotNone(m, "the clean/personal choice is gone")
        self.assertEqual(m.group(1), "y")

    def test_ssh_host_keys_are_left_out(self):
        """Every machine installed from the ISO would otherwise present the SAME host identity."""
        self.assertIn("/etc/ssh/ssh_host_*", self.fn)

    def test_saved_networks_are_left_out(self):
        for place in ("NetworkManager/system-connections", "iwd", "wpa_supplicant"):
            with self.subTest(place=place):
                self.assertIn(place, self.fn)

    def test_the_shadow_backups_go_too(self):
        """passwd- and shadow- hold exactly what the rewritten ones drop."""
        self.assertIn("/etc/shadow-", self.fn)

    def test_a_clean_image_never_keeps_a_home(self):
        """The two questions can be answered in contradiction."""
        self.assertIn('"$KEEP_HOME" = *n* || "$CLEAN" = *y*', self.fn)

    def test_it_still_autologins(self):
        """Removing the autologin gives a prompt for an account with no password set."""
        self.assertIn("--autologin live", self.fn)

    def test_the_desktop_waits_for_networkmanager(self):
        self.assertIn("After=NetworkManager.service", self.fn)
        self.assertIn("Wants=NetworkManager.service", self.fn)
        self.assertIn("NetworkManager-enable", self.fn)

    def test_the_live_user_can_become_root(self):
        """The password-locked console account still needs to run the installer as root."""
        self.assertIn("NOPASSWD", self.fn)
        self.assertIn("etc/sudoers.d/live", self.fn)

    def test_the_sudoers_drop_in_has_the_mode_sudo_demands(self):
        """sudo refuses to run AT ALL if a sudoers file has a mode it dislikes — which would lock
        the disc out of root far more thoroughly than having no drop-in."""
        i = self.fn.index('"etc/sudoers.d/live" f')
        self.assertIn("440", self.fn[i:i + 60])

    def test_it_does_not_edit_the_build_hosts_sudoers(self):
        """The squashfs replaces its policy through a pseudo-file; the running host is untouched."""
        self.assertNotRegex(self.fn, r"(?:>|sed[^\n]*|chmod[^\n]*)\s*/etc/sudoers(?:\s|$)")

    def test_the_sudoers_drop_in_is_reachable(self):
        """A correct file in sudoers.d grants nothing unless the main policy includes the directory."""
        self.assertIn("@includedir /etc/sudoers.d", self.fn)
        self.assertIn('pseudoput "etc/sudoers" f 440', self.fn)
        self.assertIn("visudo -cf", self.fn)

    def test_the_live_image_forces_the_posterchan_sway_config(self):
        """A host's stock config may name an excluded wallpaper and does not start our shell."""
        self.assertIn('pseudoput "etc/sway/config" f 644', self.fn)
        self.assertIn("pc-shell-start", self.fn)
        self.assertIn("sway -C -c", self.fn)

    def test_it_grants_only_the_live_account(self):
        # The RULE, not the first mention of the word — "NOPASSWD" appears in the comment above it
        # explaining why it is there, which is what my first version of this matched.
        rules = [l.strip() for l in self.fn.splitlines()
                 if "NOPASSWD" in l and "printf" in l]
        self.assertEqual(len(rules), 1, "expected exactly one sudoers rule, got %r" % rules)
        self.assertIn("live ALL=", rules[0])
        self.assertNotIn("ALL ALL=", rules[0])

    def test_the_live_account_has_no_password_login(self):
        """agetty preauthenticates the local console; every password-based entry stays locked."""
        self.assertIn("live:!:20000", self.fn)
        self.assertNotIn("live::20000", self.fn)

    def test_the_build_machines_root_password_is_not_in_the_iso(self):
        """The live account reaches root through its explicit sudo rule; copying the host's root
        hash into a clean image leaks a credential and leaves an unnecessary direct-login path."""
        self.assertIn("sed -i 's/^root:[^:]*/root:!/'", self.fn)

    def test_the_hostname_is_not_this_machines(self):
        self.assertIn('pseudoput "etc/hostname" f 644 0 0 echo posterchanos', self.fn)


class TheCloneToolIsStillThere(unittest.TestCase):
    """The ISO builder is a NEW option, not a replacement for anything.

    Reported as "you kinda ruined the important feature of gentoo.sh, option 6 used to let you clone
    desktop -> usb and vice versa". It had not been removed — but there are now two different [6]s,
    one per menu, and both move an operating system around. The main menu's has cloned a running
    system between a disk and a USB since the first commit; mine writes an ISO and lives under Tools
    and Tweaks. This pins the older one so a future tidy-up cannot quietly take it, and pins the
    labels apart so they cannot be confused again.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = GENTOO.read_text()

    def test_backup_restore_is_still_offered(self):
        self.assertIn("Backup/Restore Live OS", self.src)

    def test_it_still_has_a_function_behind_it(self):
        self.assertIn("liveOSrestore()", self.src)
        self.assertIn('liveOSrestore "$HARD_DISK"', self.src)

    def test_backup_to_a_server_is_still_offered(self):
        """The other direction."""
        self.assertIn("Backup OS to Build Server", self.src)
        self.assertIn("backupOS()", self.src)

    def test_option_six_means_exactly_one_thing(self):
        """The ISO builder is numbered 7 and 6 is left empty in that menu on purpose. Two [6]s that
        both move an operating system around, one menu apart, is what made a working feature look
        deleted."""
        # Only MENU ENTRIES count. A `[6]` in a comment explaining this rule is prose, and so is the
        # hint pointing at the clone tool — both name the number without being it. An entry's number
        # follows the colour escape directly, which is what tells them apart.
        sixes = [l for l in re.findall(r"(?m)^\s*echo -e .*?m\[6\][^\\]*", self.src)]
        self.assertEqual(len(sixes), 1, "there is more than one [6] menu entry again: %r" % sixes)
        self.assertIn("Backup/Restore", sixes[0])

    def test_the_iso_builder_is_not_numbered_six(self):
        i = self.src.index("Build an installable ISO")
        self.assertIn("[7]", self.src[max(0, i - 40):i])

    def test_the_dispatch_agrees_with_the_label(self):
        """A renumbered label with the old branch behind it is a menu entry that does nothing."""
        i = self.src.rindex("fixSound")
        self.assertRegex(self.src[i:i + 200], r"choice = 7 \]\]; then\s*liveCD")

    def test_the_iso_builder_points_at_the_clone_tool(self):
        i = self.src.index("Build an installable ISO")
        self.assertIn("main menu", self.src[i:i + 600])


class TheMenusSayWhatThisInstalls(unittest.TestCase):
    """"you need to rename the menus to PosterChanOS Installer".

    The script builds PosterChanOS and nothing else — the Gentoo-profile branches were taken out
    already — but the headings still announced a Gentoo installer, so the thing on screen disagreed
    with the thing being installed.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = GENTOO.read_text()

    def test_no_menu_heading_calls_this_a_gentoo_installer(self):
        for line in self.src.splitlines():
            if line.strip().startswith("echo -e") and "nstaller" in line:
                with self.subTest(line=line.strip()[:70]):
                    self.assertNotIn("Gentoo Installer", line)
                    self.assertNotIn("GENTOO CYBERPUNK", line)

    def test_the_headings_name_posterchanos(self):
        heads = [l for l in self.src.splitlines()
                 if l.strip().startswith("echo -e") and "nstaller" in l.lower()]
        self.assertTrue(heads, "the installer headings are gone — re-read this test")
        for h in heads:
            with self.subTest(head=h.strip()[:70]):
                self.assertIn("POSTERCHANOS", h.upper())


class ItFindsItsOwnFilesWhereverItIsInstalled(unittest.TestCase):
    """`$(dirname $0)` is right in a checkout and wrong once installed.

    "replace /usr/bin/gentoo.sh with the latest gentoo.sh" — at that path `dirname` is /usr/bin, so
    the script looked for /usr/bin/bin and /usr/bin/plymouth, found neither, and carried on. Nothing
    fails: the pc-* helpers are simply not copied, and the first sign is a freshly installed machine
    whose desktop has no pc-shell-start.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = GENTOO.read_text()

    def test_the_tree_is_resolved_once(self):
        self.assertIn("PCOS_TREE=", self.src)

    def test_no_use_site_still_guesses_from_argv0(self):
        """Only the RESOLVER may look at $0 — checked as a span, since the resolver's own loop
        naturally mentions it on a line that says nothing else."""
        lines = self.src.splitlines()
        start = next(i for i, l in enumerate(lines) if l.startswith('PCOS_TREE=""'))
        end = next(i for i, l in enumerate(lines) if l.startswith('[ -n "$PCOS_TREE" ]'))
        for n, line in enumerate(lines, 1):
            if 'dirname "$0"' in line and not line.strip().startswith("#"):
                if start < n <= end + 1:
                    continue                      # inside the resolver, which is its whole job
                with self.subTest(line=n):
                    self.fail("line %d derives a path from $0 outside the resolver: %s"
                              % (n, line.strip()))

    def test_it_looks_where_the_iso_puts_it(self):
        i = self.src.index("PCOS_TREE=")
        self.assertIn("/usr/local/share/posterchanos", self.src[i:i + 500],
                      "an ISO-installed copy cannot find the tree the ISO shipped")

    def test_the_helpers_and_theme_use_it(self):
        self.assertIn('"$PCOS_TREE/bin/$helper"', self.src)
        self.assertIn('"$PCOS_TREE/plymouth/posterchanos"', self.src)

    def test_a_bare_script_still_runs(self):
        """Not finding the tree must not be fatal — every use site has its own fallbacks, and an
        install from a lone script beats no install."""
        i = self.src.index("PCOS_TREE=")
        seg = self.src[i:i + 900]
        self.assertNotIn("exit 1", seg)


class TheLiveSessionActuallyStarts(unittest.TestCase):
    """An empty home is a terminal, not a desktop.

    "posterchan live cd is totally shit! it used Grub instead of systemd-boot and booted to a
     terminal, no gui"

    What starts the GUI is `~/.bash_profile` — the login shell on tty1 execs sway. Excluding /home
    and creating an empty /home/live produced an image that autologged in perfectly and dropped to a
    bash prompt. The scrub removed the operator AND the one file that starts a session, because on
    this system they live in the same directory.
    """

    @classmethod
    def setUpClass(cls):
        src = GENTOO.read_text()
        i = src.index("liveCD() {")
        cls.fn = src[i:src.index("\n}", i)]
        cls.src = src

    def test_the_live_user_gets_a_login_shell(self):
        self.assertIn("home/live/.bash_profile", self.fn,
                      "the live user's home is empty, so autologin lands on a bash prompt")

    def test_a_clean_disc_forgets_the_build_machines_admin_claim(self):
        """The OS-level claim suppresses Welcome even after browser profiles and users are gone."""
        self.assertIn("EXCLUDES+=(var/lib/posterchanos etc/sudoers.d/posterchan-admin", self.fn)
        self.assertIn("still carries this machine's administrator claim", self.fn)

    def test_clean_disc_drops_host_snapshot_jobs(self):
        """A host-user service cannot be copied onto a disc whose host user was intentionally removed."""
        self.assertIn("etc/systemd/system/boot-snapshot.service", self.fn)
        self.assertIn("etc/systemd/system/boot-snapshot.timer", self.fn)
        self.assertIn("etc/systemd/system/default.target.wants/boot-snapshot.timer", self.fn)

    def test_clean_disc_never_contains_telegram_bot_sessions(self):
        """telegram-bot-api stores mutable authorization/session state beneath /var/lib. Besides
        leaking credentials, copying it while the daemon writes td.binlog makes squashfs reread a
        moving file and yields a non-reproducible image."""
        self.assertIn("/var/lib/telegram-bot-api", self.fn)
        private = self.fn[self.fn.index("A release image is an operating system"):
                          self.fn.index("/opt is commonly", self.fn.index("A release image is an operating system"))]
        self.assertIn("EXCLUDES", private)
        self.assertIn("/var/lib/telegram-bot-api", private)

    def test_clean_disc_keeps_ssh_installed_but_disabled(self):
        self.assertIn("etc/systemd/system/multi-user.target.wants/sshd.service", self.fn)
        self.assertIn("inherited SSH or snapshot enablement", self.fn)

    def test_live_desktop_repairs_network_manager_before_welcome(self):
        i = self.fn.index('cat >"$WORK/live.bash_profile"')
        profile = self.fn[i:self.fn.index("\nPROFILE", i)]
        self.assertIn("systemctl is-active --quiet NetworkManager.service", profile)
        self.assertIn("sudo -n systemctl start NetworkManager.service", profile)
        self.assertLess(profile.index("systemctl start NetworkManager.service"), profile.index("exec sway"))

    def test_live_boot_has_a_root_owned_network_gate(self):
        self.assertIn('cat >"$WORK/live-network.service"', self.fn)
        self.assertIn("Requires=NetworkManager.service", self.fn)
        self.assertIn("Before=getty@tty1.service", self.fn)
        self.assertIn("multi-user.target.d/posterchan-live-network.conf", self.fn)

    def test_clean_disc_does_not_inherit_the_build_hosts_enabled_servers(self):
        self.assertIn("etc/systemd/system/multi-user.target.wants\n", self.fn)
        self.assertIn("multi-user.target.wants/NetworkManager.service s 777", self.fn)

    def test_it_starts_the_compositor(self):
        i = self.fn.index('cat >"$WORK/live.bash_profile"')
        profile = self.fn[i:self.fn.index("\nPROFILE", i)]
        self.assertIn("exec sway", profile)

    def test_it_only_does_so_on_the_first_tty(self):
        """A second console must still be a console."""
        i = self.fn.index('cat >"$WORK/live.bash_profile"')
        self.assertIn("XDG_VTNR", self.fn[i:i + 900])

    def test_it_matches_what_a_real_install_gets(self):
        """The live session and an installed one must not drift — both exec sway from tty1 with the
        same environment."""
        for line in ("exec sway", "XDG_SESSION_TYPE=wayland", "MOZ_ENABLE_WAYLAND=1"):
            with self.subTest(line=line):
                self.assertGreaterEqual(self.src.count(line), 2,
                                        "%r appears in only one of the two profiles" % line)

    def test_home_itself_is_not_excluded(self):
        """Each home is named individually rather than excluding /home wholesale.

        NOT because `-e home` breaks the pseudo entries — measured against squashfs-tools, it does
        not; that claim was mine and it was wrong. It is because naming them states the intent
        ("these people's files are not in the image") and leaves /home visibly in the tree that the
        session-home entries are written into."""
        self.assertNotIn("EXCLUDES+=(home)", self.fn)

    def test_every_real_home_is_still_left_out(self):
        """Leaving /home in must not leave anybody's files in it."""
        self.assertIn("for H in /home/*", self.fn)
        self.assertIn('EXCLUDES+=("${H#/}")', self.fn)

    def test_dotted_directories_under_home_are_covered(self):
        """/home/.snapshots exists on btrfs installs and a plain glob misses it."""
        self.assertIn("/home/.[!.]*", self.fn)

    def test_the_session_user_gets_a_home_whatever_was_answered(self):
        """THE REAL CAUSE of three rebuilds that booted to a terminal.

        The home and profile were emitted inside the CLEAN branch, so they only existed for the
        clean image's `live` account. Answer `n` to the clean question and the autologin stays as
        the operator — whose home is excluded anyway, because that is a SEPARATE question with its
        own default. That combination produces an image where the person who logs in has no home at
        all: bash finds no profile and gives a prompt, and `sway` typed by hand then gives a black
        screen, because Electron has nowhere to write its profile. Two symptoms, one missing
        directory, and nothing in the build said so."""
        for entry in ('home d 755 0 0',
                      'home/$SESS_USER d 755 $SESS_UID $SESS_GID',
                      'home/$SESS_USER/.bash_profile f 644 $SESS_UID $SESS_GID'):
            with self.subTest(entry=entry):
                self.assertIn(entry, self.fn)

    def test_those_entries_are_outside_the_clean_branch(self):
        """Emitted for the operator too, not only for `live`.

        Counted by walking the shell's own if/fi depth rather than by counting substrings: the old
        version compared a count of `if [[ "$CLEAN"` against a count of `fi` at two indents, which
        moves whenever anything unrelated is added nearby and says nothing about nesting.
        """
        open_clean = 0
        depth_of_clean = []
        for line in self.fn.splitlines():
            t = line.strip()
            if t.startswith("if ") and t.endswith("then"):
                open_clean += 1
                if '"$CLEAN" = *y*' in t:
                    depth_of_clean.append(open_clean)
            elif t == "fi":
                if depth_of_clean and depth_of_clean[-1] == open_clean:
                    depth_of_clean.pop()
                open_clean -= 1
            elif "home/$SESS_USER d 755" in t:
                self.assertFalse(depth_of_clean,
                                 "the session-home entries sit inside an open CLEAN branch")

    def test_the_session_user_is_read_not_guessed(self):
        """On a personal rescue disc it is whoever this machine already autologins."""
        self.assertIn("--autologin", self.fn)
        self.assertIn("SESS_USER=", self.fn)

    def test_the_build_says_who_will_log_in(self):
        """Three rebuilds went by with nothing on screen naming the account that would autologin."""
        self.assertIn("Live session logs in as", self.fn)

    def test_the_file_exists_before_the_image_is_packed(self):
        """A pseudo-file naming a path that does not exist yet is a silently missing entry."""
        write = self.src.index('cat >"$WORK/live.bash_profile"')
        pack = self.src.index('mksquashfs / "$WORK/iso/LiveOS/squashfs.img"')
        self.assertLess(write, pack)

    def test_the_live_image_injects_and_reads_back_its_desktop_launcher(self):
        """A desktop binary without its tiny wrapper boots into a valid but empty black Sway."""
        self.assertIn('pseudoput "usr/local/bin/posterchan" f 755', self.fn)
        self.assertIn('LIVE_DESKTOP_LAUNCHER="$(unsquashfs -cat', self.fn)
        self.assertIn("APPDIR=/opt/posterchan", self.fn)
        self.assertIn("$APPDIR/posterchan-desktop", self.fn)
        self.assertIn("black Sway screen", self.fn)

    def test_boot_kernel_and_driver_tree_must_match(self):
        """A mismatched fallback kernel reaches Welcome but cannot load network hardware drivers."""
        self.assertIn('find /lib/modules', self.fn)
        self.assertIn('KVER="$CANDIDATE"', self.fn)
        self.assertIn('no matching /lib/modules/$KVER tree', self.fn)
        self.assertIn('image does not contain /lib/modules/$KVER', self.fn)


class TheAccountRewriteActuallyWorks(unittest.TestCase):
    """RUN, not grepped. It fails in two opposite silent ways: leaving a person in, or dropping the
    system users the image needs to boot."""

    PASSWD = ("root:x:0:0:root:/root:/bin/bash\n"
              "bin:x:1:1:bin:/bin:/sbin/nologin\n"
              "sshd:x:22:22:sshd:/var/empty:/sbin/nologin\n"
              "verita84:x:1000:1000::/home/verita84:/bin/bash\n"
              "pc-5ac337fb7cb82127:x:1001:1001::/home/pc-5ac337fb7cb82127:/bin/bash\n"
              "nobody:x:65534:65534:nobody:/:/sbin/nologin\n")
    SHADOW = ("root:$6$realhash:19000:0:99999:7:::\n"
              "bin:!:19000::::::\n"
              "sshd:!:19000::::::\n"
              "verita84:$6$SECRETHASH:19000:0:99999:7:::\n"
              "pc-5ac337fb7cb82127:$6$OTHERHASH:19000:0:99999:7:::\n"
              "nobody:!:19000::::::\n")

    def _run(self, script, files):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for n, body in files.items():
                (d / n).write_text(body)
            r = subprocess.run(["bash", "-c", script], cwd=d, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return {n: (d / n).read_text() for n in ("passwd.out", "shadow.out") if (d / n).exists()}

    def test_people_are_dropped_and_the_system_survives(self):
        out = self._run("awk -F: '$3 < 1000 || $3 >= 65534' passwd > passwd.out",
                        {"passwd": self.PASSWD})["passwd.out"]
        self.assertNotIn("verita84", out)
        self.assertNotIn("pc-5ac337fb7cb82127", out)
        for keep in ("root:x:0:0", "bin:x:1:1", "sshd:x:22:22", "nobody:x:65534"):
            with self.subTest(keep=keep):
                self.assertIn(keep, out)

    def test_no_password_hash_of_a_real_person_survives(self):
        out = self._run(
            "awk -F: 'NR==FNR { if ($3 >= 1000 && $3 < 65534) drop[$1]; next } !($1 in drop)' "
            "passwd shadow > shadow.out",
            {"passwd": self.PASSWD, "shadow": self.SHADOW})["shadow.out"]
        self.assertNotIn("SECRETHASH", out)
        self.assertNotIn("OTHERHASH", out)
        self.assertIn("root:$6$realhash", out, "the rewrite step must retain root before locking it")

    def test_root_is_retained_but_locked_for_the_clean_image(self):
        out = self._run(
            "awk -F: 'NR==FNR { if ($3 >= 1000 && $3 < 65534) drop[$1]; next } !($1 in drop)' "
            "passwd shadow > shadow.out; sed -i 's/^root:[^:]*/root:!/' shadow.out",
            {"passwd": self.PASSWD, "shadow": self.SHADOW})["shadow.out"]
        self.assertIn("root:!:19000", out)
        self.assertNotIn("realhash", out)


if __name__ == "__main__":
    unittest.main()


class EveryReplacementActuallyReachesTheImage(unittest.TestCase):
    """MKSQUASHFS IGNORES A PSEUDO-FILE WHOSE PATH ALREADY EXISTS IN THE SOURCE.

    Measured against the real tool, not inferred:

        Pseudo file "etc/passwd" exists in source filesystem "src/etc/passwd".
        Ignoring, exclude it (-e/-ef) to override.

    One line on stdout while packing a 45GB filesystem, and the image is written successfully with
    the ORIGINAL file. Nine replacements were being dropped this way, and the 4.1GB ISO the laptop
    built proves it: /home/live was there (a NEW path, because /home is excluded, so its pseudo
    applied) while /etc/passwd carried no `live` at all and the getty override still autologged in
    the operator. agetty then logs in an account the image does not have, which is a login prompt --
    "posterchan live cd is totally shit ... booted to a terminal, no gui" -- and root stayed `!`,
    which is "my root password 123456 don't even work".

    The old self-check could not see any of it: it asked whether /home/<user>/.bash_profile existed,
    and that one was new, so it passed on every build.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = GENTOO.read_text()
        i = cls.src.index("\nliveCD() {")
        cls.fn = cls.src[i:cls.src.index("\n}", i)]

    def test_a_replacing_pseudo_file_goes_through_pseudoput(self):
        # A bare `echo "etc/... f "` line is a replacement nothing excludes, i.e. one silently
        # ignored at pack time.
        bad = [ln.strip() for ln in self.fn.splitlines()
               if re.match(r'^echo "(etc|usr|var)/\S+ f ', ln.strip())]
        self.assertEqual(bad, [], "these replace an existing file and would be ignored: " + str(bad))

    def test_every_recorded_path_is_excluded_from_the_source(self):
        self.assertIn('PSEUDO_REPLACED+=', self.fn, "nothing records what is being replaced")
        self.assertIn('for f in "${PSEUDO_REPLACED[@]}"; do EXARGS+=(-e "$f"); done', self.fn,
                      "the replaced paths are not excluded, so mksquashfs keeps the originals")

    def test_the_recording_happens_before_the_pack(self):
        self.assertLess(self.fn.index("PSEUDO_REPLACED=()"),
                        self.fn.index('mksquashfs / "$WORK/iso/LiveOS/squashfs.img"'))

    def test_the_account_and_the_autologin_are_read_BACK_OUT_of_the_image(self):
        """Existence is not contents. Every replaced file already existed, with the wrong contents,
        so the only check that can catch this reads the packed image."""
        self.assertIn("unsquashfs -cat", self.fn, "the image's contents are never read back")
        for want in ("etc/systemd/system/getty@tty1.service.d/override.conf", "etc/passwd"):
            self.assertIn(want, self.fn.split("did it actually work")[1],
                          f"{want} is never verified after packing")

    def test_it_refuses_to_build_an_iso_that_would_boot_to_a_prompt(self):
        after = self.fn.split("did it actually work")[1]
        self.assertIn("_lcd_fail", after)
        self.assertIn("login prompt, not a desktop", after)


class AnUnattendedBuildIsStillAScrubbedOne(unittest.TestCase):
    """AN UNANSWERED "CLEAN?" MUST MEAN CLEAN.

    `read -e -i "y"` pre-fills only on a terminal. Driven from a script the pre-fill does not happen,
    so a blank line leaves CLEAN empty -- and `[[ "$CLEAN" = *y* ]]` on an empty string is FALSE,
    which is the personal-rescue-disc branch. An unattended build would have quietly produced an
    image carrying this machine's accounts, ssh host keys and saved wifi passwords, with nothing on
    screen saying so.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = GENTOO.read_text()
        i = cls.src.index("\nliveCD() {")
        cls.fn = cls.src[i:cls.src.index("\n}", i)]

    def test_an_empty_answer_is_clean(self):
        self.assertIn('[ -n "$CLEAN" ] || CLEAN=y', self.fn,
                      "a blank answer falls into the personal-rescue-disc branch")

    def test_the_three_questions_can_be_answered_without_a_keyboard(self):
        for var in ("PC_ISO_OUT", "PC_ISO_HOME", "PC_ISO_CLEAN"):
            self.assertIn(var, self.fn, f"{var} cannot be set, so an unattended build must guess")

    def test_there_is_a_way_in_that_is_not_the_menu(self):
        self.assertIn('elif [ "$1" = "livecd" ]; then', self.src)

    def test_a_headless_build_returns_after_writing_the_iso(self):
        tail = self.src.split("It is a hybrid image:")[1]
        self.assertIn('[[ -t 0 ]] && read -p "Press enter key to Continue"', tail,
                      "a successful unattended build waits forever for a keyboard it has not got")


class InstallingTheLiveImageIsItsOwnJob(unittest.TestCase):
    """A LIVE ISO IS NOT A RUNNING INSTALLED SYSTEM, and `liveOSrestore` assumes it is.

    Booted from the disc that clone path fails two ways, neither of them a bug in it:

        rsync[sender] change_dir /boot failed: no such directory
        delete_file: rmdir{boot} failed: device or resource busy

    The first is that a live boot has no populated /boot to copy FROM -- the kernel came off the
    medium. The second is `--delete` trying to remove $TARGET/boot, which is the EFI partition the
    installer just mounted there. Both are the same misunderstanding: on a live medium the source of
    the SYSTEM and the source of the KERNEL are two different places.

    So this is a separate option, and `liveOSrestore` keeps its own behaviour -- "I don't want to
    break how we do liveRestore".
    """

    @classmethod
    def setUpClass(cls):
        cls.src = GENTOO.read_text()
        i = cls.src.index("\nliveISOinstall() {")
        cls.fn = cls.src[i:cls.src.index("\n}", i)]
        # CODE ONLY for the ordering checks. The comments here NAME the very commands being ordered
        # -- "before kernel-install runs" sits above the line that mints the machine-id -- so an
        # index into the raw text lands in prose and reports the opposite of the truth.
        cls.code = "\n".join(l for l in cls.fn.splitlines()
                             if not l.strip().startswith("#"))

    def test_the_old_path_is_untouched(self):
        i = self.src.index("\nliveOSrestore() {")
        old = self.src[i:self.src.index("\n}", i)]
        self.assertIn("rsync -aHAX --delete /boot/", old.replace("sudo ", ""),
                      "liveOSrestore's own /boot step was changed — that is the clone path")

    def test_the_kernel_is_looked_for_before_anything_is_written(self):
        """An install that copies 4GB and THEN finds it has no kernel has wasted the only slow step."""
        self.assertLess(self.code.index("No kernel found"), self.code.index("Copying the system"))

    def test_it_looks_where_a_live_boot_actually_keeps_the_medium(self):
        self.assertIn("/run/initramfs/live", self.fn)

    def test_the_system_copy_never_deletes(self):
        """`--delete` is what tries to remove $TARGET/boot out from under the mounted EFI
        partition. The target was just partitioned, so there is nothing to delete anyway."""
        body = self.fn[self.fn.index("Copying the system"):]
        first = body[:body.index("RC=$?")]
        self.assertNotIn("--delete", first)

    def test_it_does_not_follow_the_live_mounts(self):
        """--one-file-system is what keeps the squashfs, the overlay, the mounted ISO and $TARGET
        itself out of the copy without listing every one of them by hand."""
        self.assertIn("--one-file-system", self.fn)

    def test_the_kernel_is_copied_separately(self):
        self.assertIn("--exclude=/boot/*", self.fn)
        self.assertIn('"$KSRC"/ $TARGET/boot/', self.fn)

    def test_the_live_initramfs_is_not_installed(self):
        """IT IS THE ONE FILE ON THAT MEDIUM THAT MUST NOT TRAVEL. It is built to find a squashfs on
        a removable disc, so on a hard drive the machine boots looking for the USB stick it was
        installed from -- a failure that looks like a broken install and is a correct initramfs
        doing its job in the wrong place."""
        i = self.fn.index('"$KSRC"/ $TARGET/boot/')
        line = self.fn[self.fn.rindex("\n", 0, i):i]
        self.assertIn("--exclude='initramfs*'", line)
        self.assertIn("--exclude='initrd*'", line)

    def test_the_installed_machine_gets_its_own_identity_first(self):
        """The ISO ships an EMPTY /etc/machine-id on purpose -- a duplicated one breaks journald,
        DHCP leases and systemd-boot's own layout, so every live boot generates a fresh one. But
        this profile installs kernels the Boot Loader Spec way, where the entry token IS the
        machine-id (`/boot/<machine-id>/<version>/linux`, the layout on the machine this was built
        from). Run against an empty one, kernel-install has no directory to write into."""
        self.assertIn("systemd-machine-id-setup", self.code)
        self.assertLess(self.code.index("systemd-machine-id-setup"),
                        self.code.index('mkdir -p "$TARGET/boot/$MID/$KVER"'),
                        "the identity is minted after the kernel is installed against it")

    def test_a_real_initramfs_is_built_for_the_target(self):
        """The squashfs carries /lib/modules even though it carries no /boot, so the target has what
        dracut needs. Built INSIDE the chroot, or it describes this live session's hardware and root
        device rather than the installed machine's."""
        self.assertIn("chroot $TARGET", self.fn)
        self.assertIn("dracut --force", self.fn)
        i = self.fn.index("dracut --force")
        self.assertIn("chroot $TARGET", self.fn[max(0, i - 200):i],
                      "dracut runs outside the chroot — it would describe the live session")

    def test_the_live_account_does_not_land_on_the_installed_machine(self):
        """The disposable console account still does not belong on a machine somebody keeps."""
        self.assertIn("/^live:/d", self.fn)
        self.assertIn("sudoers.d/live", self.fn)

    def test_the_autologin_naming_it_goes_too(self):
        """An autologin naming an account that is no longer there is a login prompt -- the exact
        failure the ISO builder was fixed for."""
        self.assertIn("getty@tty1.service.d/override.conf", self.fn)

    def test_the_installed_shell_can_create_its_electron_profile(self):
        """A root-owned ~/.config makes Chromium abort before the first desktop window maps."""
        self.assertIn("chown -R posterchan:posterchan /home/posterchan", self.src)
        self.assertIn("PosterChan profile directory is not writable", self.src)

    def test_the_copy_is_checked_before_the_install_continues(self):
        self.assertIn("did not complete", self.fn)
        self.assertIn("not a mount point", self.fn)

    def test_there_is_a_way_in_that_is_not_the_menu(self):
        self.assertIn('elif [ "$1" = "install-live" ]; then', self.src)

    def test_installer_can_remount_media_detached_by_dracut(self):
        """A booted squashfs may have no /run/initramfs/live after switch-root."""
        self.assertIn("/run/posterchan-live-media", self.fn)
        self.assertIn('type="$(blkid -s TYPE -o value "$dev"', self.fn)
        self.assertIn('[ "$type" = iso9660 ] || [ "$type" = udf ]', self.fn)
        self.assertIn('sudo mount -o ro "$dev" "$media"', self.fn)

    def test_installer_can_remount_a_non_traversable_squashfs_lower_layer(self):
        self.assertIn("/run/posterchan-live-root", self.fn)
        self.assertIn('"$LIVEDIR/LiveOS/squashfs.img"', self.fn)
        self.assertIn('sudo mount -o loop,ro', self.fn)

    def test_success_is_gated_on_the_final_encrypted_boot_chain(self):
        """The bootloader can succeed and a later phase can still replace one of its files. The
        finalizer must inspect what the firmware will actually use before it locks root."""
        i = self.src.index("finalizeInstall() {")
        final = self.src[i:self.src.index("\n}", i)]
        for proof in ("BOOT_ENTRY", "rd\\.luks\\.uuid=luks-", "etc/crypttab",
                      "systemd-cryptsetup", "boot/keyfile.key"):
            self.assertIn(proof, final)
        self.assertLess(final.index("systemd-cryptsetup"), final.index("passwd -l root"))
        self.assertLess(final.index("systemd-cryptsetup"), final.index("Gentoo Installation Complete"))
