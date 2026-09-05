"""A RUNNING COMPOSITOR'S CONFIG FILE IS NOT A FILE AN UPDATE MAY WRITE.

Wayfire watches /etc/wayfire.ini and re-reads it when it changes, and a re-read that has to LOAD A
PLUGIN is not the operation that ran at startup. MEASURED on the real desk: the release that added
`view-shot` and `force-fullscreen` to the plugin list was installed at 19:24 and wayfire died at
19:24:28 -- "Fatal Wayland communication error: Broken pipe" in the shell log, then "Lost connection
to Wayland compositor", then a text console with every window gone. The identical config file
started a completely healthy session seconds later, which is what proves the content was never the
problem: applying it to a LIVE compositor was.

`update-posterchan` was doing exactly that, deliberately -- it was written to fix the opposite bug
(portage stages the new config as `._cfg0000_wayfire.ini` and the old release said "the new desktop
starts at your next login" while nothing ever applied it). The staging was right; the moment was
wrong. So the pending file is now applied by `pc-compositor-session`, at the one point in the
system's life when no compositor is reading it.

These tests RUN the shipped scripts against a stubbed /etc and a stubbed `pgrep`, because the bug is
in WHEN a line executes and no amount of reading the file catches that. Each is verified to fail
against the previous revision.
"""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
UPDATE = ROOT / "os/bin/update-posterchan"
SESSION = ROOT / "os/bin/pc-compositor-session"


def _stub_bin(tmp: Path, *, wayfire_running: bool) -> Path:
    """A PATH whose `pgrep` answers whether a compositor is up, and nothing else."""
    bindir = tmp / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    pgrep = bindir / "pgrep"
    pgrep.write_text("#!/bin/sh\nexit %d\n" % (0 if wayfire_running else 1))
    pgrep.chmod(0o755)
    return bindir


class TestUpdateDefersTheLiveConfig(unittest.TestCase):
    """The half that broke the desk."""

    def _run(self, *, wayfire_running: bool):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        etc = tmp / "etc"
        etc.mkdir()
        (etc / "wayfire.ini").write_text("# the running session's config\n")
        (etc / "._cfg0000_wayfire.ini").write_text("# the new release's config\n")
        bindir = _stub_bin(tmp, wayfire_running=wayfire_running)

        # Drive only the config-merge section, with the script's own text, against the fake /etc.
        body = UPDATE.read_text(encoding="utf-8")
        start = body.index("pc_own_config() {")
        section = body[start:]
        section = section.replace("/etc/", str(etc) + "/").replace("find /etc ", f"find {etc} ")
        script = tmp / "merge.sh"
        script.write_text("set -u\nGRN=''; YEL=''; OFF=''\n" + section)
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")
        out = subprocess.run(["sh", str(script)], capture_output=True, text=True, env=env, timeout=60)
        return etc, out

    def test_a_running_session_keeps_its_config(self):
        etc, out = self._run(wayfire_running=True)
        self.assertEqual(
            (etc / "wayfire.ini").read_text(), "# the running session's config\n",
            "update-posterchan replaced the config of a compositor that was running:\n" + out.stdout)
        self.assertTrue((etc / "._cfg0000_wayfire.ini").exists(),
                        "the pending config was consumed, so the next login cannot apply it")

    def test_it_says_the_update_is_deferred_rather_than_going_quiet(self):
        _etc, out = self._run(wayfire_running=True)
        self.assertIn("next login", out.stdout.lower(),
                      "a deferred config change that says nothing is the bug this replaced")

    def test_with_no_session_it_still_applies_immediately(self):
        """The original bug must not come back: on a machine with no compositor up, apply it now."""
        etc, out = self._run(wayfire_running=False)
        self.assertEqual(
            (etc / "wayfire.ini").read_text(), "# the new release's config\n",
            "the config was not applied even with no session running:\n" + out.stdout)
        self.assertFalse((etc / "._cfg0000_wayfire.ini").exists())


class TestTheSessionAppliesItAtStartup(unittest.TestCase):
    """The other half: deferring is only safe if something later actually applies it."""

    def test_the_pending_config_is_applied_before_wayfire_is_started(self):
        body = SESSION.read_text(encoding="utf-8")
        launch = body.index('wayfire -c "$session_cfg"')
        apply_at = body.index("._cfg[0-9][0-9][0-9][0-9]_wayfire.ini")
        self.assertLess(apply_at, launch,
                        "the pending config is applied after wayfire has already read the old one")

    def test_it_really_moves_the_file(self):
        """Run the loop itself, so a glob that matches nothing cannot pass as 'applied'."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        etc = tmp / "etc"
        etc.mkdir()
        (etc / "wayfire.ini").write_text("old\n")
        (etc / "._cfg0000_wayfire.ini").write_text("new\n")
        body = SESSION.read_text(encoding="utf-8")
        start = body.index("\t\tfor pending in /etc/._cfg")
        end = body.index("\t\twayfire -c ", start)
        loop = textwrap.dedent(body[start:end]).replace("/etc/", str(etc) + "/")
        script = tmp / "apply.sh"
        script.write_text("set -u\n" + loop)
        subprocess.run(["sh", str(script)], check=True, capture_output=True, text=True, timeout=60)
        self.assertEqual((etc / "wayfire.ini").read_text(), "new\n")
        self.assertFalse((etc / "._cfg0000_wayfire.ini").exists())
        self.assertEqual((etc / "wayfire.ini.pre-update").read_text(), "old\n",
                         "the previous config was not kept beside it")


class TestTheCompositorReadsACopy(unittest.TestCase):
    """AND THE STAGED-CONFIG PATH IS ONLY HALF OF IT.

    Portage stages a protected file as `._cfg0000_` only when the copy on disk was MODIFIED since it
    was installed. Nobody hand-edits /etc/wayfire.ini, so an ordinary upgrade writes straight over
    it, under a running compositor, with no staging involved -- which is what actually happened at
    19:24, and again an hour later with no `._cfg` anywhere on the machine. Deferring the staged
    copy would have prevented neither.

    So the running compositor reads a copy in the session's own runtime directory. The installed
    file stays the source of truth and is re-read at every start, which is what keeps an upgrade
    taking effect at the next login.
    """

    def test_wayfire_is_not_pointed_at_the_package_owned_path(self):
        body = SESSION.read_text(encoding="utf-8")
        launch = [ln for ln in body.splitlines() if ln.strip().startswith("wayfire -c ")]
        self.assertEqual(len(launch), 1, launch)
        self.assertNotIn("/etc/wayfire.ini", launch[0],
                         "the compositor still reads the file the package manager overwrites")

    def test_the_copy_lives_in_the_session_runtime_dir(self):
        body = SESSION.read_text(encoding="utf-8")
        self.assertIn('session_cfg="$XDG_RUNTIME_DIR/posterchan-wayfire.ini"', body,
                      "the copy would outlive the login, or be shared between sessions")

    def test_the_copy_is_taken_from_the_installed_file_every_start(self):
        """Otherwise an upgrade would never take effect and the update message would be a lie."""
        body = SESSION.read_text(encoding="utf-8")
        block = body[body.index("session_cfg="):body.index("wayfire -c ")]
        self.assertIn("/etc/wayfire.ini", block)
        self.assertIn("cp -f", block)

    def test_a_failed_copy_still_starts_a_desktop(self):
        """A full or read-only runtime dir must not be the reason somebody has no session."""
        body = SESSION.read_text(encoding="utf-8")
        block = body[body.index("session_cfg="):body.index("wayfire -c ")]
        self.assertIn("if ! cp", block)
        self.assertIn('session_cfg="${PC_WAYFIRE_CONFIG:-/etc/wayfire.ini}"', block)

    def test_the_pending_apply_still_runs_first(self):
        """Both halves, in the only order that works: apply what portage staged, then copy."""
        body = SESSION.read_text(encoding="utf-8")
        self.assertLess(body.index("._cfg[0-9][0-9][0-9][0-9]_wayfire.ini"),
                        body.index("session_cfg="),
                        "the copy is taken before the staged config is applied to the source")


class TestThePackagedCopiesAgree(unittest.TestCase):
    def test_both_scripts_match_their_overlay_copies(self):
        for name in ("pc-compositor-session", "update-posterchan"):
            src = (ROOT / "os/bin" / name).read_bytes()
            packaged = (ROOT / "os/overlay/app-misc/posterchanos-shell/files" / name).read_bytes()
            self.assertEqual(src, packaged, f"{name} differs from the copy the ebuild installs")


if __name__ == "__main__":
    unittest.main()
