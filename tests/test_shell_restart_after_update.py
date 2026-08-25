"""Ctrl+Alt+Backspace must tell a wedged renderer from a REPLACED one.

"ctrl alt backspace after update always shows 2 black screens now."

`pc-shell-restart` sends sway a `pc:restart` tick, and the shell reloads the PAGE inside a process
that keeps running. That is right for a wedged renderer and wrong the moment the code on disk is no
longer the code the process started from: `update-posterchan` renames /opt/posterchan aside and
deletes it, and `emerge` replaces the files in place. A reload then loads the NEW bundle into the
OLD main process — a mismatched preload, a protocol handler reading an archive whose header it has
already cached — and both monitors go black with nothing in any log. A stale process cannot be
reloaded, only replaced.

THE OTHER HALF, and the reason this is not simply "always restart": a restart closes every window
the person has open, and a reload costs a second. So the expensive answer is earned by POSITIVE
evidence only. No /proc, an unreadable link, a pid that has already gone — all reload.

THIS FILE RUNS THE SCRIPT. A grep for `readlink` would pass against a check wired to the wrong half
of an `if`, and staleness here is measured off real inodes — so each case starts a real process from
a real binary and really deletes or replaces it. Only `swaymsg`, `pgrep` and the launcher are stubs,
because they are the outputs being measured.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "os", "bin", "pc-shell-restart")
PACKAGED = os.path.join(ROOT, "os", "overlay", "app-misc", "posterchanos-shell",
                        "files", "pc-shell-restart")

# A tiny real program to BE the desktop: it must outlive the script's own run so that
# /proc/<pid>/exe is a live link to look at.
SLEEPER = "import time\ntime.sleep(120)\n"


@unittest.skipUnless(sys.platform.startswith("linux") and os.path.isdir("/proc"),
                     "staleness is measured off /proc/<pid>/exe")
class ShellRestartTellsAReloadFromAReplacement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pc-shell-restart-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.proc = None
        # Stubs. Each records that it ran, so the assertion is about what the script DID.
        self._stub("swaymsg", 'printf "%s\\n" "$*" >> "$PC_TEST_DIR/swaymsg.log"\n')
        self._stub("pc-shell-start", 'echo started >> "$PC_TEST_DIR/start.log"\n')
        # pgrep answers with whatever the test parked in pids.txt (the script greps for a
        # /opt/posterchan path that cannot exist on this machine).
        self._stub("pgrep", 'cat "$PC_TEST_DIR/pids.txt" 2>/dev/null; '
                            '[ -s "$PC_TEST_DIR/pids.txt" ]\n')

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.send_signal(signal.SIGKILL)
                self.proc.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----------------------------------------------------------------------------
    def _stub(self, name, body):
        p = os.path.join(self.bin, name)
        with open(p, "w") as fh:
            fh.write("#!/bin/sh\n" + body)
        os.chmod(p, 0o755)

    def _start_desktop(self):
        """A real process running a real binary we own, so it can really be replaced."""
        exe = os.path.join(self.tmp, "posterchan-desktop")
        shutil.copy2(sys.executable, exe)
        proc = subprocess.Popen([exe, "-c", SLEEPER],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.proc = proc
        for _ in range(100):                       # wait for /proc/<pid>/exe to exist
            if os.path.exists(f"/proc/{proc.pid}/exe"):
                break
            time.sleep(0.02)
        self._pids(str(proc.pid))
        return exe, proc

    def _pids(self, text):
        with open(os.path.join(self.tmp, "pids.txt"), "w") as fh:
            fh.write(text + ("\n" if text else ""))

    def _run(self, script=SCRIPT):
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env["PATH"]
        env["PC_TEST_DIR"] = self.tmp
        env["PC_SHELL_START"] = os.path.join(self.bin, "pc-shell-start")
        return subprocess.run(["sh", script], env=env, capture_output=True,
                              text=True, timeout=60)

    def _log(self, name):
        p = os.path.join(self.tmp, name)
        return open(p).read() if os.path.exists(p) else ""

    def _ticked(self):
        return "pc:restart" in self._log("swaymsg.log")

    def _relaunched(self):
        return "started" in self._log("start.log")

    # ---- the two answers --------------------------------------------------------------------
    def test_a_live_desktop_whose_binary_is_intact_is_only_reloaded(self):
        """The ordinary case, and the one that must stay cheap: nobody's windows are closed."""
        self._start_desktop()
        self._run()
        self.assertTrue(self._ticked(), "a healthy desktop was not sent the reload tick")
        self.assertFalse(self._relaunched(),
                         "a healthy desktop was restarted, which closes every open window")

    def test_a_desktop_whose_binary_was_deleted_is_replaced(self):
        """What `update-posterchan` does: /opt/posterchan is renamed aside and then removed."""
        exe, proc = self._start_desktop()
        os.unlink(exe)
        self._run()
        self.assertFalse(self._ticked(),
                         "a reload was sent into a process running code that no longer exists")
        self.assertTrue(self._relaunched(), "the stale desktop was not replaced")
        self.assertIsNotNone(proc.poll() or proc.wait(timeout=10) if True else None)

    def test_a_desktop_whose_binary_was_replaced_in_place_is_replaced(self):
        """What `emerge` does: a new file renamed over the old path. Same path, new inode — the
        case a "does the file still exist?" check answers wrongly, because it does."""
        exe, _ = self._start_desktop()
        fresh = exe + ".new"
        shutil.copy2(sys.executable, fresh)
        os.rename(fresh, exe)                       # new inode at the same path
        self._run()
        self.assertFalse(self._ticked(), "a replaced desktop was reloaded rather than restarted")
        self.assertTrue(self._relaunched(), "the replaced desktop was not restarted")

    def test_the_stale_process_is_actually_stopped_before_the_relaunch(self):
        """Otherwise the launcher races the old instance for the singleton socket, which is the
        failure the tick was introduced to avoid in the first place."""
        exe, proc = self._start_desktop()
        os.unlink(exe)
        self._run()
        self.assertIsNotNone(proc.poll(), "the old desktop is still running beside the new one")

    # ---- and the rule that keeps it from being trigger-happy ---------------------------------
    def test_no_desktop_at_all_goes_straight_to_the_launcher(self):
        self._pids("")
        self._run()
        self.assertFalse(self._ticked(), "a tick was sent to a shell that is not running")
        self.assertTrue(self._relaunched())

    def test_a_pid_that_cannot_be_measured_is_reloaded_not_restarted(self):
        """"I could not ask" is never "it is stale". A restart closes every window, so it is
        earned by positive evidence only — here pgrep names a pid with no /proc entry, which is
        what a process exiting between the two calls looks like."""
        self._pids("2147483646")                    # far above any live pid
        self._run()
        self.assertTrue(self._ticked(),
                        "an unmeasurable pid closed every window the person had open")
        self.assertFalse(self._relaunched())

    # ---- the two copies -----------------------------------------------------------------------
    def test_both_copies_of_the_helper_are_the_same_file(self):
        """os/bin is what the installer drops in; files/ is what the ebuild installs. Every helper
        here exists twice and a fix applied to one of them is a fix that reaches half the machines."""
        with open(SCRIPT) as a, open(PACKAGED) as b:
            self.assertEqual(a.read(), b.read(),
                             "os/bin and the shell package's copy have drifted")

    def test_the_packaged_copy_behaves_the_same(self):
        """Read as text they are equal; run, they had better be. Cheap, and it catches the case
        where the comparison above is relaxed later."""
        self._start_desktop()
        self._run(PACKAGED)
        self.assertTrue(self._ticked())
        self.assertFalse(self._relaunched())


if __name__ == "__main__":
    unittest.main()
