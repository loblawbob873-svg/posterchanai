"""`update-posterchan` — one command that updates the app and the session together.

Asked for as "a bash alias to update posterchan". It is a file in /usr/local/bin instead, because an
alias exists only inside an interactive bash: invisible to `sudo`, to a .desktop entry, to a script,
and over ssh, which are most of the ways somebody would reach for it.

The things it has to get right are all things that were learned the hard way in this repo: the
overlay IS the release channel (a machine that has not synced cannot see a new build), the app and
the session are two packages that must move together, and /etc/sway/config belongs to Portage — an
etc-update replaces a hand-edited one, which is what silently reverted the key bindings during
development.
"""
import os
import re
import shutil
import stat
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CMD = ROOT / "os" / "bin" / "update-posterchan"
PACKAGED_CMD = ROOT / "os/overlay/app-misc/posterchanos-shell/files/update-posterchan"
GENTOO = ROOT / "os" / "gentoo.sh"
EBUILD = ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild"


class TheCommandExists(unittest.TestCase):
    def test_it_is_there_and_executable(self):
        self.assertTrue(CMD.exists())
        self.assertTrue(os.stat(CMD).st_mode & stat.S_IXUSR, "not executable")

    @unittest.skipIf(shutil.which("bash") is None, "no bash")
    def test_it_parses(self):
        r = subprocess.run(["bash", "-n", str(CMD)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_installer_and_packaged_helpers_are_identical(self):
        """The direct installer and Portage package must execute the same updater."""
        self.assertEqual(CMD.read_bytes(), PACKAGED_CMD.read_bytes())

    def test_both_installers_ship_it(self):
        """Installed by the script AND by the ebuild — a machine built either way must have it."""
        for f in (GENTOO, EBUILD):
            with self.subTest(file=f.name):
                self.assertIn("update-posterchan", f.read_text())

    def test_the_overlay_owns_the_current_installer(self):
        """Updating PosterChanOS must update its recovery/LiveUSB tool as well."""
        ebuild = EBUILD.read_text()
        publish = (ROOT / "scripts" / "publish_overlay.sh").read_text()
        self.assertIn('dobin "${FILESDIR}/gentoo.sh"', ebuild)
        self.assertIn('$(dirname "$SRC")/gentoo.sh', publish)
        self.assertIn('posterchanos-shell-${SHELL_VER}.ebuild', publish)

    def test_privileged_helpers_land_where_the_desktop_and_sudoers_call_them(self):
        ebuild = EBUILD.read_text()
        self.assertIn("exeinto /usr/local/bin", ebuild)
        self.assertIn('doexe "${FILESDIR}/${helper}"', ebuild)

    def test_bundled_tor_remains_executable_in_every_hard_disk_install_path(self):
        """Captured on the installed package: Tor existed at 0644 and first-run reported EACCES."""
        desktop_ebuilds = sorted((ROOT / "os/overlay/app-misc/posterchan-desktop").glob("*.ebuild"))
        self.assertTrue(desktop_ebuilds)
        ebuild = desktop_ebuilds[-1].read_text()
        installer = GENTOO.read_text()
        updater = CMD.read_text()
        workflow = (ROOT / ".github/workflows/desktop.yml").read_text()
        tor = "/opt/posterchan/resources/tor/tor/tor"
        self.assertIn(f"fperms 0755 {tor}", ebuild)
        self.assertGreaterEqual(installer.count(f"chmod 0755 {tor}"), 2)
        self.assertIn('chmod 0755 "$NEW/resources/tor/tor/tor"', updater)
        self.assertIn("test -x dist/linux-unpacked/resources/tor/tor/tor", workflow)

    def test_desktop_package_owns_the_wrapper_path_the_installer_puts_first(self):
        desktop_ebuilds = sorted((ROOT / "os/overlay/app-misc/posterchan-desktop").glob("*.ebuild"))
        ebuild = desktop_ebuilds[-1].read_text()
        self.assertIn("exeinto /usr/local/bin", ebuild)
        self.assertIn("newexe - posterchan", ebuild)
        self.assertNotIn("newbin - posterchan", ebuild)

    def test_privileged_helper_rules_are_package_owned(self):
        """An update must make multi-user login work, not only a fresh gentoo.sh install."""
        ebuild = EBUILD.read_text()
        self.assertIn("posterchan-provision.sudoers", ebuild)
        self.assertIn("posterchan-session-switch.sudoers", ebuild)
        self.assertIn("fperms 0440 /etc/sudoers.d/posterchan-provision", ebuild)
        self.assertIn("fperms 0440 /etc/sudoers.d/posterchan-session-switch", ebuild)
        files = EBUILD.parent / "files"
        self.assertIn("/usr/local/bin/pc-provision-user",
                      (files / "posterchan-provision.sudoers").read_text())
        self.assertIn("/usr/local/bin/pc-session-switch *",
                      (files / "posterchan-session-switch.sudoers").read_text())

    def test_session_switch_reloads_the_changed_getty_dropin(self):
        """Restart alone reuses systemd's cached ExecStart and logs back into the old user."""
        helper = (EBUILD.parent / "files" / "pc-session-switch").read_text()
        reload_at = helper.index("systemctl daemon-reload")
        restart_at = helper.index("systemd-run --quiet", reload_at)
        self.assertLess(reload_at, restart_at)


class ItUpdatesBothHalves(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = CMD.read_text()

    def test_it_updates_the_app_and_the_session(self):
        """Updating one alone leaves the desktop and the app that draws it out of step — a key
        binding sending a tick nothing listens for, or a helper the app cannot find."""
        self.assertIn("app-misc/posterchan-desktop", self.src)
        self.assertIn("app-misc/posterchanos-shell", self.src)

    def test_it_syncs_the_overlay_first(self):
        """The overlay is the release channel; without a sync there is nothing new to find."""
        self.assertIn("emaint sync -r posterchan", self.src)

    def test_concurrent_updates_cannot_corrupt_the_overlay_checkout(self):
        """Portage does not protect Git object writes from two simultaneous updater commands."""
        lock = self.src.index("exec 9>/run/lock/posterchan-update.lock")
        held = self.src.index("flock 9", lock)
        sync = self.src.index("emaint sync -r posterchan")
        emerge = self.src.index("emerge -u", sync)
        self.assertLess(lock, held)
        self.assertLess(held, sync)
        self.assertLess(held, emerge)

    def test_it_does_not_sync_the_whole_tree(self):
        """A full `emerge --sync` is a large fetch with nothing to do with this app, and making
        people wait for it is how a one-command update becomes one nobody runs.

        Checked against the CODE, not the file: the comment above the sync explains what it is not
        doing, and naming the thing you avoid is not doing it. Matching prose is how these guards
        rot, and it has done so four times today."""
        code = "\n".join(l for l in self.src.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("emerge --sync", code)

    def test_an_unreachable_overlay_is_not_fatal(self):
        i = self.src.index("if ! emaint sync -r posterchan")
        self.assertIn("Could not reach the overlay", self.src[i:i + 1200])

    def test_a_restored_overlay_repairs_only_portages_checkout(self):
        """A recreated release repository may have unrelated history. The updater may reset the
        disposable Portage checkout, but must never apply that recovery to a user directory."""
        self.assertIn('git -C "$repo" fetch --prune origin main', self.src)
        self.assertIn('git -C "$repo" reset --hard FETCH_HEAD', self.src)
        self.assertNotIn("git reset --hard /", self.src)

    def test_a_missing_or_colliding_overlay_is_rebuilt_before_replacement(self):
        repair = self.src[self.src.index("_pc_repair_overlay()"):
                          self.src.index("# ---------------------------------------------------------------- how this machine")]
        self.assertIn('mktemp -d "$parent/.posterchan-repair.XXXXXX"', repair)
        self.assertIn('git clone -q https://gentoo.poster.place/posterchan-overlay.git', repair)
        self.assertIn('if [ -e "$repo" ]', repair)
        self.assertLess(repair.index('git clone -q'), repair.index('mv "$repo" "$backup"'))
        self.assertIn('mv "$backup" "$repo"', repair)

    def test_direct_release_is_audited_before_it_can_replace_opt(self):
        audit = self.src.index('_pc_audit_desktop_asar "$NEW/resources/app.asar"')
        install = self.src.index('mv "$NEW" /opt/posterchan')
        self.assertLess(audit, install)
        for asset in ("concord.js", "cord-reader.js", "code.js", "hostfiles.js",
                      "preview.js", "wm.js", "clipboard.js"):
            self.assertIn(asset, self.src)
        for marker in ("messages-communities", "openHostFile", "openSyncCodeFile",
                       "function taskbarMove(w)", "let _altSwitch=null"):
            self.assertIn(marker, self.src)

    def test_a_stale_lan_proxy_retries_the_same_tls_host_through_public_dns(self):
        """A cached LAN 404 must not make a restored public overlay look permanently absent."""
        self.assertIn("_pc_fetch_overlay_public_dns", self.src)
        self.assertIn("https://cloudflare-dns.com/dns-query?name=gentoo.poster.place&type=A",
                      self.src)
        self.assertIn("http.curloptResolve=gentoo.poster.place:443:$ip", self.src)
        self.assertIn("|| _pc_fetch_overlay_public_dns", self.src)
        self.assertNotIn("http.sslVerify=false", self.src)


class ItBehavesLikeACommandSomebodyTypes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = CMD.read_text()

    def test_it_asks_for_root_rather_than_refusing(self):
        """"Run this with sudo" is a worse answer than a password prompt."""
        self.assertRegex(self.src, r'exec sudo -- "\$0"')

    def test_it_says_when_nothing_changed(self):
        """"Updated" printed over a no-op is the same lie as a progress bar that finishes without
        doing anything."""
        self.assertIn("Already up to date", self.src)
        self.assertIn("before", self.src)

    def test_a_failed_update_says_the_desktop_is_untouched(self):
        i = self.src.index("The update failed")
        self.assertIn("untouched", self.src[i:i + 160])

    def test_it_does_not_restart_the_desktop_itself(self):
        """On PosterChanOS the shell IS the desktop: restarting it closes every window, and doing
        that inside an update somebody ran mid-task is how an update becomes something they avoid."""
        body = self.src
        self.assertNotRegex(body, r"(?m)^\s*(pkill|swaymsg exec)\s")
        self.assertIn("The new desktop starts at your next login", body)

    def test_it_warns_that_portage_owns_the_sway_config(self):
        self.assertIn("/etc/sway/config", self.src)
        self.assertIn("etc-update", self.src)


if __name__ == "__main__":
    unittest.main()


def test_the_updater_applies_the_config_files_it_owns_and_leaves_the_rest():
    """AN UPDATE THAT ONLY MENTIONS THE NEW CONFIG HAS NOT APPLIED IT.

    Measured on a real update: the new /etc/wayfire.ini landed as `._cfg0000_wayfire.ini` and the
    running session kept the old one — so a release that added Alt+Tab, changed what Alt+F4 closes
    and started a notification daemon installed cleanly, said "the new desktop starts at your next
    login", and changed nothing at the next login either. The only way to notice was to go looking
    for a file nobody had mentioned.

    RUN, not read: the block is extracted and driven against a fixture tree, because the failure
    modes here are a `basename`/`sed` that strips the wrong prefix and a `case` that matches too
    much — neither of which a grep over the source can see.
    """
    import subprocess
    import tempfile
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    src = (root / "os/bin/update-posterchan").read_text(encoding="utf-8")
    block = src.split("pc_own_config()", 1)[1]
    block = "pc_own_config()" + block[:block.index("\nfi\n") + 4]

    with tempfile.TemporaryDirectory() as td:
        fixture = Path(td)
        for rel, old, new in (
            ("etc/wayfire.ini", "old wayfire", "new wayfire"),
            ("etc/xdg/mako/config", "old mako", "new mako"),
            ("etc/xdg/xdg-desktop-portal/portals.conf", "old portals", "new portals"),
            # Another package's update is somebody else's decision and must be left alone.
            ("etc/otherpkg/thing.conf", "old other", "new other"),
        ):
            path = fixture / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(old)
            (path.parent / ("._cfg0000_" + path.name)).write_text(new)

        # NO COMPOSITOR RUNNING, and the test has to SAY so rather than inherit it from whatever
        # machine is running the suite. The updater defers /etc/wayfire.ini while a session is up
        # (writing it under a live Wayfire is what killed one), and it answers that question with
        # `pgrep` -- so on a developer's own desktop, or a build box that happens to have a session,
        # this test measured the machine instead of the script. Stub it both ways below.
        stub = fixture / "bin"
        stub.mkdir(parents=True, exist_ok=True)
        (stub / "pgrep").write_text("#!/bin/sh\nexit 1\n")     # nothing is running
        (stub / "pgrep").chmod(0o755)
        env = dict(os.environ, PATH=f"{stub}:{os.environ['PATH']}")

        script = "GRN='' YEL='' OFF=''\n" + block.replace("/etc", str(fixture / "etc"))
        got = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                             env=env)
        assert got.returncode == 0, got.stderr

        assert (fixture / "etc/wayfire.ini").read_text() == "new wayfire"
        assert (fixture / "etc/xdg/mako/config").read_text() == "new mako"
        assert (fixture / "etc/xdg/xdg-desktop-portal/portals.conf").read_text() == "new portals"
        # THE OLD ONE IS KEPT. These are package-owned files an `etc-update --automode -5` would
        # have discarded outright; a hand edit somebody wants back is worth one backup.
        assert (fixture / "etc/wayfire.ini.pre-update").read_text() == "old wayfire"
        # And nothing else was touched, nor its pending update consumed.
        assert (fixture / "etc/otherpkg/thing.conf").read_text() == "old other"
        assert (fixture / "etc/otherpkg/._cfg0000_thing.conf").is_file()
        assert "1 other config update" in got.stdout

        # WITH A SESSION UP, the compositor's own config is left for the next login and said so by
        # name -- writing it under a live Wayfire is what ended a login at a text console. The other
        # package-owned files are not the compositor's and are still applied.
        (fixture / "etc/wayfire.ini").write_text("old wayfire")
        (fixture / "etc/._cfg0000_wayfire.ini").write_text("new wayfire")
        (fixture / "etc/xdg/mako/config").write_text("old mako")
        (fixture / "etc/xdg/mako/._cfg0000_config").write_text("new mako")
        (stub / "pgrep").write_text("#!/bin/sh\nexit 0\n")     # a compositor IS running
        (stub / "pgrep").chmod(0o755)
        live = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                              env=env)
        assert live.returncode == 0, live.stderr
        assert (fixture / "etc/wayfire.ini").read_text() == "old wayfire", (
            "the updater rewrote the config of a compositor that was running")
        assert (fixture / "etc/._cfg0000_wayfire.ini").is_file(), (
            "the pending config was consumed, so the next login cannot apply it")
        assert "next login" in live.stdout, "a deferred config change that says nothing is the bug"
        assert (fixture / "etc/xdg/mako/config").read_text() == "new mako", (
            "an unrelated package-owned file was deferred along with the compositor's")


def test_the_updater_no_longer_talks_about_sway():
    src = (ROOT / "os/bin/update-posterchan").read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "sway" not in code, code
