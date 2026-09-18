"""A TV THAT FLAGS NO PREFERRED MODE MUST NOT GET 640x480, AND ONE DID.

Measured 2026-09-17 on a TCL 55S451 (55" TV) over DisplayPort, from the machine's own `wayfire.log`:

    Detected modes:
      1920x1080 @ 60.000 Hz          <- first of 27
      ... 25 more ...
      640x480 @ 59.929 Hz            <- last
    connector DP-5: Modesetting with 640x480 @ 59.929 Hz

`wlr_output_preferred_mode()` returns the mode the EDID flags PREFERRED and, when none is flagged,
**the last mode in the list**. This TV flags none — confirmed at runtime, where `wlr-randr` marks one
mode `(current)` and not one `(preferred)` — so the desktop came up at 640x480 on a 1080p panel.
Reported as "resolution was weird", which it was.

THE FIXTURE IS THE REAL OUTPUT. `wlr-randr` is stubbed with the exact text that machine printed,
including its four 1920x1080 entries at different refresh rates and the absence of any `preferred`
marker, because the whole bug is a missing word in that output.

The rule is deliberately narrow and every case below pins one edge of it: act only when nothing is
preferred AND the current mode is smaller than the largest offered; never downgrade; leave a panel
that states a preference alone, since its own preference beats a pixel count.
"""
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION = (ROOT / "os/bin/pc-compositor-session").read_text(encoding="utf-8")

# The TV, verbatim (trimmed to the modes that matter; the real list is 27 long and has no `preferred`).
TV = """DP-5 "Technical Concepts Ltd 55S451 (DP-5)"
  Make: Technical Concepts Ltd
  Model: 55S451
  Enabled: yes
  Modes:
    1920x1080 px, 60.000000 Hz
    1920x1080 px, 59.938999 Hz
    1280x720 px, 60.000000 Hz
    800x600 px, 60.317001 Hz
    640x480 px, 59.929001 Hz (current)
"""

# An ordinary monitor: it says what it wants, and nothing here may override that.
MONITOR = """DP-1 "Dell U2414H"
  Modes:
    1920x1080 px, 60.000000 Hz (preferred, current)
    1280x720 px, 60.000000 Hz
"""

# A PANEL THAT PREFERS SOMETHING SMALLER THAN ITS MAXIMUM. This is the fixture that isolates the
# `preferred` check, and its absence let a mutation through: with MONITOR above, the current mode IS
# the largest, so ignoring the flag entirely still produced no action and the test passed. Real
# displays do this — a TV that advertises a mode its scaler handles badly and asks for a lower one —
# and overriding it would hand somebody a worse picture than the one the panel requested.
PREFERS_LOWER = """HDMI-A-2 "Careful Panel"
  Modes:
    1920x1080 px, 60.000000 Hz
    1280x720 px, 60.000000 Hz (preferred, current)
"""

# Already at its best, with no preferred flag: there is nothing to do and no reason to touch it.
BEST_ALREADY = """HDMI-A-1 "No Name"
  Modes:
    1920x1080 px, 60.000000 Hz (current)
    640x480 px, 59.929001 Hz
"""

TWO_OUTPUTS = TV + MONITOR


def chooser() -> str:
    """`pc_best_unpreferred_modes`, sliced out of the shipped session script."""
    start = SESSION.index("pc_best_unpreferred_modes() {")
    end = SESSION.index("\npc_fix_unpreferred_modes()", start)
    return SESSION[start:end]


class UnpreferredMode(unittest.TestCase):
    def decide(self, randr_output):
        """Run the shipped chooser against a stubbed wlr-randr; returns its lines."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            stub = bin_dir / "wlr-randr"
            stub.write_text("#!/bin/sh\ncat <<'PC_EOF'\n" + randr_output + "PC_EOF\n")
            stub.chmod(0o755)
            script = (f'export PATH="{bin_dir}:$PATH"\n' + chooser()
                      + "\npc_best_unpreferred_modes\n")
            r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
            self.assertEqual("", r.stderr.strip(), r.stderr)
            return [l for l in r.stdout.splitlines() if l.strip()]

    def test_the_tv_is_moved_to_its_largest_mode(self):
        lines = self.decide(TV)
        self.assertEqual(1, len(lines), lines)
        out, res, hz = lines[0].split()
        self.assertEqual("DP-5", out)
        self.assertEqual("1920x1080", res, "it did not pick the largest mode the TV offers")
        self.assertEqual("60.000000", hz, "at equal size it must take the highest refresh")

    def test_a_monitor_that_states_a_preference_is_left_alone(self):
        self.assertEqual([], self.decide(MONITOR),
                         "overrode a display that declared its own preferred mode")

    def test_a_panel_that_prefers_a_lower_mode_keeps_it(self):
        """The one case that isolates the `preferred` check — see PREFERS_LOWER."""
        self.assertEqual([], self.decide(PREFERS_LOWER),
                         "upgraded a panel past the mode it explicitly asked for")

    def test_an_output_already_at_its_largest_is_left_alone(self):
        self.assertEqual([], self.decide(BEST_ALREADY),
                         "would have re-set a mode that was already the best available")

    def test_each_output_is_judged_on_its_own(self):
        """A machine with both must fix the TV and not touch the monitor."""
        lines = self.decide(TWO_OUTPUTS)
        self.assertEqual(1, len(lines), lines)
        self.assertTrue(lines[0].startswith("DP-5 1920x1080"), lines)

    def test_nothing_happens_without_wlr_randr(self):
        """The helper is absent on a minimal image; that must be silence, not an error."""
        script = ('export PATH=/nonexistent\n' + chooser() + "\npc_best_unpreferred_modes; echo RC=$?\n")
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
        self.assertIn("RC=0", r.stdout, r.stdout + r.stderr)
        self.assertNotIn("x", r.stdout.replace("RC=0", ""), "it proposed a mode with no way to read one")

    def test_the_fix_is_opt_outable_and_never_delays_the_session(self):
        self.assertIn("PC_NO_MODE_FIX", SESSION, "no way to turn the override off")
        # Backgrounded at the call site: a display needing no change must not cost the login a second,
        # and wlr-randr cannot answer until the compositor does.
        self.assertRegex(
            SESSION, r"\(\s*pc_wait_for_wayland[^)]*&&\s*pc_fix_unpreferred_modes\s*\)\s*&",
            "the mode fix is not run in the background after the compositor starts")


if __name__ == "__main__":
    unittest.main()
