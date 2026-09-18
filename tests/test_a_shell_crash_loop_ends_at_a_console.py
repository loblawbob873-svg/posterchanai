"""A DESKTOP SHELL THAT CRASHES FOR EVER MUST END SOMEWHERE THAT CAN EXPLAIN ITSELF.

Reported 2026-09-18 from a television, after the resolution was fixed:

    "desktop resolution looks better but it still keeps restarting at Join a Network non-stop"

THE SHAPE, AND WHY EVERY GUARD ALREADY IN PLACE MISSED IT. `pc-compositor-session` supervises the
shell as a replaceable generation: when the shell exits it waits a bounded interval for the launcher
to publish a NEW pid, and if one appears it says so and goes round again. That is right — a
deliberate restart (Ctrl+Alt+Backspace, the post-update reload) must not take the compositor down
with it. But the loop had **no cap**, so a shell that dies on its own, repeatedly, was answered with
a fresh shell, repeatedly, for as long as the machine stayed on.

Wayfire itself is perfectly healthy throughout. So:

  * `rescue()` never runs — the session never fails.
  * The `.bash_profile` restart-loop guard never trips — the session never exits.
  * `wayfire.log` says nothing wrong, because nothing is wrong with the compositor.

The only record is `note "shell replaced by pid N; Wayfire kept."`, written over and over into
compositor-fallback.log, which nothing printed. Three separate reports of "it keeps reloading the
desktop" produced no evidence for exactly that reason.

AND WHY IT LOOKS LIKE THE WIFI SCREEN. Each replacement is a brand-new page with nothing decided on
it, so the first-run wizard draws its first unanswered step. "Restarting at Join a Network" is not
the network step failing; it is the shell dying and the wizard being the first thing a fresh page
has to show.

THE RULE: a crash loop stops, and the console says so, with the log of the half that actually broke.
A DELIBERATE restart is not a crash and must never count — the marker `pc-shell-restart` leaves is
what tells them apart (`limit` is 600 with it, 30 without), and counting restarts would drop somebody
to a console for pressing a key five times.
"""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION = (ROOT / "os/bin/pc-compositor-session").read_text(encoding="utf-8")
PACKAGED = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-compositor-session"


def supervision_loop():
    """The shell-generation loop, sliced out of the shipped script."""
    start = SESSION.index("\t\t\tpc_gens=0 pc_gen_first=0")
    end = SESSION.index("\t\t\tpc_stop_wayfire", start)
    return SESSION[start:end]


class TheLoopIsBounded(unittest.TestCase):
    def test_a_crash_loop_has_a_limit_at_all(self):
        loop = supervision_loop()
        self.assertIn("PC_SHELL_CRASH_LIMIT", loop,
                      "the shell supervision loop will restart a crashing shell for ever; Wayfire "
                      "stays healthy so nothing else in the session can notice")
        self.assertRegex(loop, r"pc_gens=\$\(\(pc_gens \+ 1\)\)")
        self.assertIn("break", loop)

    def test_a_deliberate_restart_is_not_counted_as_a_crash(self):
        """`limit` is 600 when pc-shell-restart left its marker. That is a person, not a fault."""
        loop = supervision_loop()
        self.assertRegex(loop, r'if \[ "\$limit" -le 30 \]',
                         "deliberate restarts are counted toward the crash limit, so five presses "
                         "of Ctrl+Alt+Backspace would drop somebody to a console")

    def test_the_window_is_bounded_too(self):
        """Five crashes across a fortnight of uptime is not a crash loop."""
        loop = supervision_loop()
        self.assertIn("PC_SHELL_CRASH_WINDOW", loop,
                      "a crash count with no time window would eventually trip on any long-lived "
                      "machine that had five unrelated shell crashes")

    def test_the_reason_names_the_count_and_the_span(self):
        loop = supervision_loop()
        reasons = re.findall(r'reason="([^"]*)"', loop)
        self.assertTrue(reasons, "the crash loop breaks without recording a reason")
        crash = [r for r in reasons if "pc_gens" in r]
        self.assertTrue(crash, f"no reason mentions the crash count; found {reasons}")
        self.assertIn("${pc_span}", crash[0])


class ItRuns(unittest.TestCase):
    """RUN the loop against a fake shell that keeps dying. A shell test that only greps is a guess."""

    def _drive(self, crashes, marker=False, limit_env=None):
        """Replay `crashes` shell generations and report whether the loop broke, and after how many."""
        loop = supervision_loop()
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            pidfile = tmp / "shell.pid"
            if marker:
                (tmp / "posterchan-shell-restarting").touch()
            # A harness that stands in for: a live compositor, a pid file the "launcher" rewrites
            # with a fresh dead-but-once-live pid each round, and note() as a recorder.
            script = f"""
set -u
XDG_RUNTIME_DIR="{tmp}"
PC_WAYFIRE_SHELL_PID_FILE="{pidfile}"
PC_SHELL_CRASH_LIMIT={limit_env or 5}
PC_SHELL_CRASH_WINDOW=300
wayfire_pid=$$
shell_pid=''
limit=30
reason=''
NOTES="{tmp}/notes"
: > "$NOTES"
note(){{ printf '%s\\n' "$*" >> "$NOTES"; }}
# Each generation: publish a pid that is alive for one check then gone. `sleep` gives us a real
# process whose pid we can publish and then kill.
GEN=0
MAXGEN={crashes}
publish(){{
  GEN=$((GEN + 1))
  [ "$GEN" -le "$MAXGEN" ] || return 1
  sleep 30 & echo $! > "$PC_WAYFIRE_SHELL_PID_FILE"
  ( sleep 0.3; kill $! 2>/dev/null ) &
}}
# Stub the two things the loop uses to observe the world.
publish
{loop}
echo "BROKE_AFTER=$pc_gens"
echo "REASON=${{reason:-none}}"
grep -c 'shell replaced' "$NOTES" 2>/dev/null | sed 's/^/REPLACED=/'
"""
            # The loop reads the pid file itself; the harness re-publishes on each round by
            # replacing the file from a background ticker.
            ticker = f"""
( for i in $(seq 1 {crashes}); do
    sleep 0.6
    sleep 30 & p=$!; echo $p > "{pidfile}"
    ( sleep 0.3; kill $p 2>/dev/null ) &
  done ) &
"""
            script = script.replace("publish\n" + loop, ticker + loop)
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
            return r.stdout, r.stderr

    def test_it_stops_instead_of_cycling_for_ever(self):
        out, err = self._drive(crashes=12, limit_env=4)
        m = re.search(r"BROKE_AFTER=(\d+)", out)
        self.assertIsNotNone(m, f"the loop never reported: {out!r} {err[-300:]!r}")
        self.assertGreaterEqual(int(m.group(1)), 1,
                                "the loop exited without counting a single crash")
        self.assertIn("crashed and was restarted", out,
                      f"it stopped without saying it was a crash loop: {out!r}")

    def test_the_reason_reaches_the_session(self):
        out, _ = self._drive(crashes=12, limit_env=4)
        self.assertRegex(out, r"REASON=the desktop shell crashed and was restarted \d+ times in \d+s")


class TheConsoleGetsTheRightLog(unittest.TestCase):
    """For a SHELL crash, wayfire.log is the half that is fine."""

    def test_rescue_prints_the_shell_log_and_not_only_its_path(self):
        fn = SESSION[SESSION.index("rescue() {"):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertRegex(fn, r'tail -n \d+ "\$HOME/\.config/posterchan-desktop/shell\.log"',
                         "the rescue screen names the shell log without printing it — and a shell "
                         "crash loop leaves nothing in wayfire.log at all")

    def test_rescue_prints_the_sessions_own_commentary(self):
        """`shell replaced by pid N; Wayfire kept.` repeated IS the crash loop, named."""
        fn = SESSION[SESSION.index("rescue() {"):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertRegex(fn, r'tail -n \d+ "\$state_dir/compositor-fallback\.log"',
                         "the one file that records the crash loop is never shown")

    def test_an_absent_log_prints_no_empty_banner(self):
        fn = SESSION[SESSION.index("rescue() {"):]
        fn = fn[:fn.index("\n}\n") + 3]
        self.assertIn('if [ -s "$HOME/.config/posterchan-desktop/shell.log" ]', fn)
        self.assertIn('if [ -s "$state_dir/compositor-fallback.log" ]', fn)

    def test_the_packaged_copy_is_the_same_file(self):
        self.assertEqual(SESSION, PACKAGED.read_text(encoding="utf-8"),
                         "the ISO ships the packaged copy, so a fix only in os/bin/ never boots")


if __name__ == "__main__":
    unittest.main()
