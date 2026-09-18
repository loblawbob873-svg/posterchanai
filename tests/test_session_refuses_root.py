"""`Wayfire exited with status 1` is one cause, and the session must name it before it tries.

WHAT WAS MEASURED, and why this test runs the script instead of reading it.

A LiveUSB came back as *"wayfire exited with status 1"* and nothing else. The release ISO
(sha256 0ca85976…) was booted in a KVM guest on real hardware and every failure this session can
produce was provoked against the shipped wayfire 0.10.1 / wlroots 0.19.2:

    no DRM device at all      -> `Found 0 GPUs, cannot create backend` -> SIGSEGV -> status 255
    XDG_RUNTIME_DIR missing   -> wayfire's fatal handler               -> status 255
    an unreadable config file -> wayfire's fatal handler               -> status 255
    the session stops Wayfire -> `Got SIGTERM, shutting down`          -> status 0
    started as root           -> `Unable to drop root ... refusing to start` -> STATUS 1

and of the five `EXIT_FAILURE` returns in wayfire's `main.cpp` only that one is reachable from
here: `-c <file>` is a recognised argument, the "Failed to open DRM render device" return is
compiled out (the shipped binary carries the udmabuf `Trying SW rendering instead.` warning and not
the LOGE that returns), renderer creation falls back to pixman rather than failing (measured — a
bochs-drm guest with no render node reached the desktop), and the configuration backend is
installed.  So status 1 has exactly one meaning and the screen was printing the number.

THE SECOND HALF IS WHY IT REPEATS.  The rescue screen's own instruction is `exec
/usr/local/bin/pc-compositor-session`, and the reflex answer to "the desktop will not start" is to
run that with sudo — which cannot ever work and produces the same unreadable screen again.

A grep-for-`id -u` test would pass against a check wired after the compositor launch, or against one
whose message never reaches the screen.  These RUN the shipped script with a fake `id`, a fake
`wayfire` that records whether it was called at all, and `PC_RESCUE_SHELL` pointed at `/bin/true`.
"""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SESSION = ROOT / "os/bin/pc-compositor-session"
PACKAGED = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-compositor-session"


class SessionRoot(unittest.TestCase):
    def _run(self, uid, with_wayfire=True):
        """Run the session script with `id` reporting `uid`, and report what happened.

        `with_wayfire=False` removes the executable, which is the cheapest way to reach the rescue
        screen as an ORDINARY user — the branch that still has to tell somebody how to try again.
        """
        work = Path(tempfile.mkdtemp(prefix="pc-session-root-"))
        fake = work / "bin"
        fake.mkdir()
        (fake / "id").write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "  -u|-ru) echo %s ;;\n"
            "  -un) echo %s ;;\n"
            "  *) echo %s ;;\n"
            "esac\n" % (uid, "root" if uid == "0" else "live", uid)
        )
        # A wayfire that leaves a trace. The point of the check is that it is never reached.
        if with_wayfire:
            (fake / "wayfire").write_text(
                "#!/bin/sh\ntouch %s\n[ \"$1\" = --version ] && exit 0\nsleep 5\n"
                % (work / "wayfire-ran")
            )
        for child in fake.iterdir():
            child.chmod(0o755)
        home = work / "home"
        (home / ".config").mkdir(parents=True)
        env = dict(os.environ)
        env.update(
            HOME=str(home),
            XDG_STATE_HOME=str(work / "state"),
            XDG_CONFIG_HOME=str(home / ".config"),
            XDG_RUNTIME_DIR=str(work / "run"),
            PC_RESCUE_SHELL="/bin/true",
            # A CLOSED PATH, so a `wayfire` installed on the machine running the tests can
            # never stand in for the one this fixture deliberately left out.
            PATH=str(fake) + os.pathsep + "/usr/bin" + os.pathsep + "/bin",
        )
        (work / "run").mkdir()
        proc = subprocess.run(
            ["/bin/sh", str(SESSION)], env=env, capture_output=True, text=True, timeout=120,
        )
        return proc, (work / "wayfire-ran").exists()

    def test_a_root_session_is_refused_by_name_before_wayfire_is_started(self):
        proc, wayfire_ran = self._run("0")
        out = proc.stdout + proc.stderr
        self.assertIn("could not start", out,
                      "a root session must reach the rescue screen:\n" + out)
        self.assertIn("root", out.lower())
        self.assertIn("without sudo", out.lower(),
                      "the screen must say the thing that fixes it, not only the thing that is "
                      "wrong:\n" + out)
        self.assertFalse(
            wayfire_ran,
            "the refusal must come BEFORE Wayfire is launched — a reason known in advance must "
            "never be reported to the user as an exit status.")

    def test_an_ordinary_user_is_not_refused(self):
        """The guard must not be a new way to have no desktop."""
        proc, _ran = self._run("1000")
        out = proc.stdout + proc.stderr
        self.assertNotIn("running as root", out,
                         "a normal login must never see the root refusal:\n" + out)

    def test_an_ordinary_users_rescue_still_says_how_to_try_again_and_not_with_sudo(self):
        """The retry instruction is the screen's only way out; the guard must not have eaten it."""
        proc, _ran = self._run("1000", with_wayfire=False)
        out = proc.stdout + proc.stderr
        self.assertIn("could not start", out, out)
        self.assertTrue(
            [l for l in out.splitlines()
             if "exec /usr/local/bin/pc-compositor-session" in l],
            "the rescue screen must still say how to start the session again:\n" + out)
        self.assertIn("not with sudo", out.lower(),
                      "and it must say which way does NOT work:\n" + out)

    def test_root_is_not_told_to_run_it_as_root(self):
        """The first draft printed `run it as root` TO root — advice that cannot ever work."""
        proc, _ran = self._run("0")
        out = proc.stdout + proc.stderr
        self.assertNotIn("run it as root", out.lower(), out)
        self.assertIn("ordinary user", out.lower(),
                      "root has to be pointed at an account that can succeed:\n" + out)

    def test_a_refusal_before_the_launch_claims_nothing_about_the_gpu(self):
        """An empty log means two different things either side of the launch.

        Saying "Wayfire died before it wrote anything ... could not open a GPU" about a compositor
        that was never started sends somebody hunting a graphics driver over a login account.
        """
        proc, _ran = self._run("0")
        out = proc.stdout + proc.stderr
        self.assertNotIn("could not open a GPU", out, out)
        self.assertIn("never started", out.lower(), out)

    def test_the_packaged_copy_has_not_diverged(self):
        """The overlay ships its own copy; a fix in one of them is a fix on neither machine."""
        self.assertEqual(SESSION.read_text(), PACKAGED.read_text(),
                         "os/bin/pc-compositor-session and the packaged copy have diverged")


if __name__ == "__main__":
    unittest.main()
