"""A DESKTOP THAT DIES MUST PRINT WHY, NOT NAME A FILE NOBODY CAN READ.

Reported three times from the same television, and every report arrived with no evidence:

    "wayfire just crashed to terminal"
    "i keeps reloading desktop"
    "restarted desktop"                       <- and, again, nothing to go on

`pc-compositor-session`'s `rescue()` already gets this right: when the session cannot START it
prints the last lines of wayfire.log to the console, with a comment explaining that naming a path
asks somebody in front of a broken machine to know to `cat` it. That rule was then not applied one
level up. The `.bash_profile` restart-loop guard — the path taken when the desktop DOES start and
then dies, which is the case actually being reported — printed:

    PosterChanOS stopped a graphical-session restart loop.
    Diagnostics: $HOME/.local/state/posterchanos/wayfire.log

On a LIVE USB that file is in tmpfs and dies with the boot, so the one copy of the reason was
destroyed by the reboot that came next. Three round trips to a television produced three reports of
"it restarted" and zero lines of log, which is why the crash is still undiagnosed while the
resolution beside it was fixed from a log that WAS printed.

There are THREE copies of that guard in gentoo.sh — the installed system's profile
(`finalizeInstall`), `posterchanShell`'s, and the live session's (`liveCD`) — and the live one is the
one a person boots off a stick. A fix to one of them is not a fix.
"""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENTOO = (ROOT / "os/gentoo.sh").read_text(encoding="utf-8")
GUARD = "PosterChanOS stopped a graphical-session restart loop."


def guards():
    """Every copy of the restart-loop guard, from its message to the `fi` that ends it."""
    out = []
    for m in re.finditer(re.escape(GUARD), GENTOO):
        start = GENTOO.rindex('if [ "$pc_attempts" -gt 2 ]; then', 0, m.start())
        end = GENTOO.index("\texec /usr/local/bin/pc-compositor-session", m.end())
        out.append(GENTOO[start:end])
    return out


class EveryCopyPrintsTheReason(unittest.TestCase):
    def test_there_are_still_three_of_them(self):
        """If this count changes, the copies below are not the whole story any more."""
        self.assertEqual(3, len(guards()),
                         "the number of restart-loop guards changed; find the new one and check it "
                         "prints its log too")

    def test_each_one_prints_the_log_and_does_not_merely_name_it(self):
        for i, g in enumerate(guards()):
            with self.subTest(copy=i):
                self.assertIn("wayfire.log", g, "the guard does not mention the compositor log")
                self.assertRegex(g, r"tail -n \d+ \"\$pc_wlog\"",
                                 "the guard names the log without printing it — on a live USB that "
                                 "file dies with the boot, so the reason is lost")

    def test_each_one_shows_the_shell_log_too(self):
        """A compositor that lives while the SHELL dies is the other half of 'it restarted'."""
        for i, g in enumerate(guards()):
            with self.subTest(copy=i):
                self.assertIn("shell.log", g)

    def test_an_empty_log_prints_nothing_rather_than_an_empty_banner(self):
        for i, g in enumerate(guards()):
            with self.subTest(copy=i):
                self.assertRegex(g, r'if \[ -s "\$pc_wlog" \]',
                                 "it would print a '--- last lines ---' banner over nothing")

    def test_the_live_session_is_one_of_them(self):
        """The stick is what gets booted on somebody else's television."""
        live = GENTOO.index("cat >\"$WORK/live.bash_profile\"")
        nxt = GENTOO.index("\nPROFILE\n", live)
        self.assertIn(GUARD, GENTOO[live:nxt], "the live profile has no restart-loop guard at all")
        self.assertIn("tail -n", GENTOO[live:nxt],
                      "the LIVE session — the one booted from a USB, whose logs do not survive a "
                      "reboot — is the copy that does not print its log")


class ItActuallyRuns(unittest.TestCase):
    """RUN the guard. A quoted heredoc is easy to get subtly wrong and impossible to see."""

    def _run(self, wayfire_log, shell_log):
        live = GENTOO.index("cat >\"$WORK/live.bash_profile\"")
        body = GENTOO[GENTOO.index("\n", live) + 1:GENTOO.index("\nPROFILE\n", live)]
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            state = home / ".local/state/posterchanos"
            state.mkdir(parents=True)
            if wayfire_log is not None:
                (state / "wayfire.log").write_text(wayfire_log)
            if shell_log is not None:
                d = home / ".config/posterchan-desktop"
                d.mkdir(parents=True)
                (d / "shell.log").write_text(shell_log)
            # Third attempt on this boot: the guard must trip and explain itself.
            (state / "compositor-boot-attempt").write_text("BOOTID 5\n")
            script = (f'HOME="{home}"\nXDG_VTNR=1\nWAYLAND_DISPLAY=\n'
                      'cat(){ echo BOOTID; }\n'          # /proc/.../boot_id, stubbed
                      + body)
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
            return r.stdout

    def test_a_real_crash_reason_reaches_the_screen(self):
        out = self._run("EGL: failed to create a context\nwlr: could not open DRM device\n", None)
        self.assertIn("stopped a graphical-session restart loop", out)
        self.assertIn("could not open DRM device", out,
                      "the reason the desktop died was written to a log and never shown; that is "
                      "three trips to a television with nothing to report")

    def test_the_shell_log_reaches_the_screen_too(self):
        out = self._run("x\n", "Electron: render process gone (crashed)\n")
        self.assertIn("render process gone", out)

    def test_no_log_at_all_is_not_an_empty_banner(self):
        out = self._run(None, None)
        self.assertIn("stopped a graphical-session restart loop", out)
        self.assertNotIn("--- last lines of wayfire.log ---", out)


if __name__ == "__main__":
    unittest.main()
