"""THE DESKTOP MUST BE LAID OUT AT THE RIGHT SIZE, NOT RESIZED AFTER IT IS DRAWN.

Reported three times from the same TV, and the middle report is the one that matters:

    "resolution was weird and wayfire just crashed to terminal"
    "the desktop has a terrible resolution where it's only showing in the center of the TV and
     super high"                                              <- after the FIRST attempt at a fix
    "resolution is complete ass still"                        <- after that fix was made opt-in

`wlr_output_preferred_mode()` returns the mode the EDID flags PREFERRED and, when none is flagged,
the LAST mode in the list. This TV (a TCL 55S451) flags none, so a 1080p panel came up at 640x480.

THE FIRST ATTEMPT PICKED THE RIGHT MODE AT THE WRONG MOMENT. It drove `wlr-randr` after the
compositor AND the shell were already running, so the output grew underneath a desktop that had
already laid itself out for 640x480 — which is precisely a small desktop in the middle of a large
picture. The mode was never the problem; changing it at that point was. Making it opt-in then handed
the TV back its 640x480, which is why the third report says nothing changed.

THE KERNEL ALREADY ANSWERS THIS, AND NOTHING WAS ASKING IT. `drm_mode_sort` puts the EDID's
preferred mode first when there is one and its own ranking first when there is not, so line 1 of
/sys/class/drm/<card>-<connector>/modes is either the same answer wlroots reaches (a no-op) or the
answer wlroots missed (the fix). Crucially it is readable with NO COMPOSITOR RUNNING, so it can be
written into wayfire.ini before wayfire ever reads it and the desktop is laid out once, correctly.

Measured on this laptop's panel, which DOES flag a preference:

    $ head -2 /sys/class/drm/card0-eDP-1/modes
    1920x1080
    1680x1050

The fixtures below are sysfs trees, because sysfs is what the shipped code reads.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION = (ROOT / "os/bin/pc-compositor-session").read_text(encoding="utf-8")
PACKAGED = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-compositor-session"


def _fn(name: str) -> str:
    start = SESSION.index(f"{name}() {{")
    return SESSION[start:SESSION.index("\n}\n", start) + 3]


def _harness(extra: str) -> str:
    """The two shipped functions, plus the globals they read from the top of the script."""
    return ("set -u\n"
            'drm_root=${PC_DRM_ROOT:-/sys/class/drm}\n'
            "pc_say(){ printf 'SAY %s\\n' \"$*\"; }\n"
            + _fn("pc_kernel_preferred_modes") + _fn("pc_pin_preferred_modes") + extra)


def sysfs(conns):
    """Build a fake /sys/class/drm. `conns` maps connector -> (status, [modes])."""
    tmp = tempfile.mkdtemp()
    for name, (status, modes) in conns.items():
        d = Path(tmp, f"card0-{name}")
        d.mkdir(parents=True)
        (d / "status").write_text(status + "\n")
        (d / "modes").write_text("".join(m + "\n" for m in modes))
    # A card directory with no connector suffix sits under the same glob; it is not an output.
    Path(tmp, "card0").mkdir()
    return tmp


# The TV, as the kernel sees it. No EDID preferred flag, so the kernel's own ranking leads and
# wlroots' "last in the list" trails — 1920x1080 against 640x480.
TV = {"DP-5": ("connected", ["1920x1080", "1920x1080", "1280x720", "800x600", "640x480"])}
# A panel that states a preference: the kernel puts it first, so pinning it changes nothing.
LAPTOP = {"eDP-1": ("connected", ["1920x1080", "1680x1050", "1280x1024"])}


class WhatTheKernelPrefers(unittest.TestCase):
    def modes(self, conns):
        root = sysfs(conns)
        r = subprocess.run(["bash", "-c", _harness("\npc_kernel_preferred_modes\n")],
                           capture_output=True, text=True, timeout=30,
                           env={**os.environ, "PC_DRM_ROOT": root})
        self.assertEqual("", r.stderr.strip(), r.stderr)
        return [l.split() for l in r.stdout.splitlines() if l.strip()]

    def test_the_tv_reports_its_largest_mode_not_its_last(self):
        """THE REPORTED MACHINE. wlroots would take 640x480 off the end of this same list."""
        self.assertEqual([["DP-5", "1920x1080"]], self.modes(TV))

    def test_a_disconnected_output_is_not_an_output(self):
        conns = dict(TV)
        conns["HDMI-A-1"] = ("disconnected", [])
        self.assertEqual([["DP-5", "1920x1080"]], self.modes(conns))

    def test_a_card_is_not_a_connector(self):
        """`card0` and `card0-DP-5` sit under the same glob; only one of them is an output."""
        out = self.modes(TV)
        self.assertTrue(all(name != "card0" for name, _ in out), out)

    def test_an_output_with_no_modes_is_skipped_silently(self):
        self.assertEqual([], self.modes({"DP-1": ("connected", [])}))

    def test_an_interlaced_mode_is_refused(self):
        """A config naming a mode wayfire cannot parse is worse than saying nothing at all."""
        self.assertEqual([], self.modes({"DP-1": ("connected", ["1920x1080i", "1280x720"])}))

    def test_no_sysfs_at_all_is_silence_not_an_error(self):
        r = subprocess.run(["bash", "-c", _harness("\npc_kernel_preferred_modes; echo RC=$?\n")],
                           capture_output=True, text=True, timeout=30,
                           env={**os.environ, "PC_DRM_ROOT": "/nonexistent"})
        self.assertIn("RC=0", r.stdout)
        self.assertNotIn("x", r.stdout.replace("RC=0", ""))


class WhatGetsWrittenToWayfireIni(unittest.TestCase):
    def pin(self, conns, ini, env=None):
        root = sysfs(conns)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp, "wayfire.ini")
            cfg.write_text(ini)
            r = subprocess.run(["bash", "-c", _harness('\npc_pin_preferred_modes "$1"\n'), "_", str(cfg)],
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "PC_DRM_ROOT": root, **(env or {})})
            self.assertEqual(0, r.returncode, r.stderr)
            return cfg.read_text(), r.stdout

    def test_the_tv_is_pinned_before_wayfire_reads_the_file(self):
        """THE FIX. The mode is in the config, so the desktop is laid out once, at 1920x1080."""
        out, _ = self.pin(TV, "[core]\nplugins = posterchan-shell\n")
        self.assertIn("[output:DP-5]", out)
        self.assertRegex(out, r"\[output:DP-5\]\s*\nmode\s*=\s*1920x1080")

    def test_the_rest_of_the_config_is_untouched(self):
        original = "[core]\nplugins = posterchan-shell view-shot\n\n[idle]\ndpms_timeout = 600\n"
        out, _ = self.pin(TV, original)
        self.assertTrue(out.startswith(original),
                        "pinning a mode rewrote settings it has no business touching")

    def test_an_operator_who_set_their_own_mode_wins(self):
        """Somebody who configured an output has said something this cannot improve on."""
        original = "[core]\nplugins = x\n\n[output:DP-5]\nmode = 1280x720\n"
        out, log = self.pin(TV, original)
        self.assertEqual(original, out, "overwrote a mode the operator chose")
        self.assertIn("already configured", log, "it did not say why it stood down")

    def test_a_panel_that_states_its_own_preference_is_a_no_op_in_effect(self):
        """Pinning what wlroots would already pick changes nothing about the picture."""
        out, _ = self.pin(LAPTOP, "[core]\nplugins = x\n")
        self.assertRegex(out, r"\[output:eDP-1\]\s*\nmode\s*=\s*1920x1080")

    def test_it_can_be_turned_off(self):
        out, _ = self.pin(TV, "[core]\nplugins = x\n", env={"PC_NO_MODE_PIN": "1"})
        self.assertNotIn("[output:", out)

    def test_a_config_it_cannot_write_is_left_alone(self):
        """The fallback config is /etc/wayfire.ini, owned by root; refuse rather than fail loudly."""
        root = sysfs(TV)
        r = subprocess.run(["bash", "-c", _harness('\npc_pin_preferred_modes /nonexistent/w.ini; echo RC=$?\n')],
                           capture_output=True, text=True, timeout=30,
                           env={**os.environ, "PC_DRM_ROOT": root})
        self.assertIn("RC=0", r.stdout)


class ItRunsBeforeTheCompositor(unittest.TestCase):
    """THE WHOLE POINT. Pinning after wayfire has drawn is the bug it replaces, not the fix."""

    def test_the_pin_is_called_before_wayfire_is_started(self):
        pin = SESSION.index('pc_pin_preferred_modes "$session_cfg"')
        start = SESSION.index('wayfire -c "$session_cfg"')
        self.assertLess(pin, start,
                        "the mode is pinned after wayfire is launched — that is the resize-after-"
                        "layout bug that put a small desktop in the middle of a big TV")

    def test_it_writes_the_config_wayfire_is_actually_given(self):
        """A pin into a file nobody reads is worse than none: it looks fixed and is not."""
        between = SESSION[SESSION.index('pc_pin_preferred_modes "$session_cfg"'):
                          SESSION.index('wayfire -c "$session_cfg"')]
        self.assertNotIn("session_cfg=", between,
                         "session_cfg is reassigned between the pin and the launch")

    def test_the_packaged_copy_is_the_same_file(self):
        """The image installs the overlay's copy; a fix only in os/bin/ never reaches a TV."""
        self.assertTrue(PACKAGED.exists(), f"{PACKAGED} is missing")
        self.assertEqual(SESSION, PACKAGED.read_text(encoding="utf-8"),
                         "os/bin/pc-compositor-session and the packaged copy have drifted — the ISO "
                         "ships the packaged one, so the desktop would boot without this fix")


if __name__ == "__main__":
    unittest.main()
