"""The media keys and the on-screen controls must agree about their limits.

`pc-key` is a shell script and the UI goes through desktop/audio.js and desktop/power.js. That
duplication is deliberate — a keybinding fires several times a second while a key is held, on a
machine where the shell may not even be running yet, and spawning an Electron process per keypress
is not a thing to do. Both sides drive the same underlying tools.

What duplication costs is DRIFT, and drift here is invisible: the slider and the key disagree about
what "maximum" means, and whichever the person used last is the one that was wrong. Worse for
brightness — the UI refuses to go below 1% because 0 is OFF on most panels, and a key that does not
share that refusal turns the screen off with no way to turn it back on.

So the numbers are pinned. If either side moves, this fails and names both.
"""
import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY = os.path.join(ROOT, "os", "bin", "pc-key")
AUDIO = os.path.join(ROOT, "desktop", "audio.js")
POWER = os.path.join(ROOT, "desktop", "power.js")
SH = os.path.join(ROOT, "os", "gentoo.sh")


@unittest.skipIf(not os.path.exists(KEY), "no pc-key here")
class KeyLimits(unittest.TestCase):
    def setUp(self):
        self.key = open(KEY, encoding="utf-8").read()
        self.audio = open(AUDIO, encoding="utf-8").read()
        self.power = open(POWER, encoding="utf-8").read()

    def test_it_is_valid_shell(self):
        r = subprocess.run(["bash", "-n", KEY], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-400:])

    def test_the_volume_ceiling_matches_the_module(self):
        """Above this is software gain that is distortion with a volume number attached."""
        js = re.search(r"^const MAX = ([0-9.]+);", self.audio, re.M)
        sh = re.search(r"^VOL_MAX=([0-9.]+)", self.key, re.M)
        self.assertTrue(js and sh, "one of the two limits is no longer where this test looks")
        self.assertEqual(float(js.group(1)), float(sh.group(1)),
                         "the volume key and the volume slider disagree about maximum")

    def test_the_brightness_floor_matches_the_module(self):
        """0 is OFF on most panels, not dim — and somebody who cannot see the screen cannot undo
        what they just did. A key that does not share the floor is that failure with a shortcut."""
        js = re.search(r"^const MIN_PERCENT = (\d+);", self.power, re.M)
        sh = re.search(r"^BRIGHT_MIN=(\d+)", self.key, re.M)
        self.assertTrue(js and sh, "one of the two floors is no longer where this test looks")
        self.assertEqual(int(js.group(1)), int(sh.group(1)),
                         "the brightness key can dim below what the slider allows")

    def test_the_key_actually_passes_the_ceiling_to_wpctl(self):
        """A constant that is declared and not used is a comment."""
        self.assertIn('-l "$VOL_MAX"', self.key,
                      "the ceiling is never given to wpctl — volume-up can exceed it")

    def test_the_brightness_floor_is_applied_before_writing(self):
        self.assertIn('[ "$new" -lt "$BRIGHT_MIN" ] && new=$BRIGHT_MIN', self.key)

    def test_every_key_the_config_binds_is_a_verb_the_script_knows(self):
        """A binding naming an action pc-key does not handle is a key that silently does nothing —
        it exits 2 to a compositor that shows no output."""
        from tests.wayfire_config import CONFIG
        cfg = CONFIG.read_text(encoding="utf-8")
        bound = set(re.findall(r"/usr/local/bin/pc-key ([a-z-]+)", cfg))
        self.assertTrue(bound, "no media keys are bound at all")
        known = set(re.findall(r"^\s{4}([a-z-]+)\)\s", self.key, re.M))
        self.assertTrue(bound <= known, f"bound but unhandled: {sorted(bound - known)}")

    def test_the_helper_is_shipped_by_the_installer(self):
        cfg = open(SH, encoding="utf-8").read()
        helper_loop = re.search(r"for helper in ([^\n]+); do", cfg)
        self.assertIsNotNone(helper_loop, "the installer has no support-helper copy loop")
        self.assertIn("pc-key", helper_loop.group(1),
                      "pc-key is bound in the config but never copied to the machine")
        self.assertIn('cp -f "$PCOS_TREE/bin/$helper"', cfg,
                      "the packaged support tree is not copied into the installed system")

    def test_the_whole_media_and_brightness_set_is_bound(self):
        """NINE KEYS, AND THIS SESSION SHIPPED WITH NONE OF THEM.

        Sway bound all of them; not one was carried over, so volume, mute, mic-mute, brightness and
        the transport keys did nothing on every keyboard that has them. They are also deliberately
        NOT gated on the session being unlocked — Sway spelled that `--locked`, Wayfire runs command
        bindings regardless, and a laptop whose volume keys stop at the lock screen is one somebody
        will hold the power button on.
        """
        from tests.wayfire_config import bindings
        binds = bindings()
        for chord, verb in (("KEY_VOLUMEUP", "volume-up"), ("KEY_VOLUMEDOWN", "volume-down"),
                            ("KEY_MUTE", "mute"), ("KEY_MICMUTE", "mic-mute"),
                            ("KEY_BRIGHTNESSUP", "brightness-up"),
                            ("KEY_BRIGHTNESSDOWN", "brightness-down"),
                            ("KEY_PLAYPAUSE", "play-pause"), ("KEY_NEXTSONG", "next"),
                            ("KEY_PREVIOUSSONG", "previous")):
            self.assertIn(chord, binds, f"{chord} is not bound")
            self.assertIn(f"pc-key {verb}", binds[chord],
                          f"{chord} runs {binds[chord]!r}, not pc-key {verb}")

    def test_mute_toggles_on_a_key_and_is_explicit_in_the_ui(self):
        """Opposite answers to the same question, both correct: a key press has no state of its own
        to disagree with, whereas a BUTTON that toggles blind fights whatever else changed the mute
        — another app, a headset, a second window."""
        self.assertIn("set-mute \"$SINK\" toggle", self.key)
        self.assertIn("set-mute', id, on ? '1' : '0'", self.audio.replace('"', "'"))


if __name__ == "__main__":
    unittest.main()
