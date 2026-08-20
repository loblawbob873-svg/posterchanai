"""Power, brightness, battery and the audio mixer — RUN against a fake /sys and a stub wpctl.

"Like a real desktop OS" is four different mechanisms with four different failures: brightness is a
sysfs file, sleep is a systemd verb behind polkit, profiles are a daemon that may not be installed,
and a battery is a directory that does not exist on a tower. Each is asked for separately, and
absent hardware is reported as ABSENT rather than as an error — a desktop machine has no backlight,
and that is not a fault to show anybody.

The two that could hurt someone are tested hardest: a brightness slider that can reach zero on a
panel where zero means OFF, and a volume percentage passed to a tool that expects a fraction.
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POWER = os.path.join(ROOT, "desktop", "power.js")
AUDIO = os.path.join(ROOT, "desktop", "audio.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "no node on this node")
class Power(unittest.TestCase):
    def setUp(self):
        self.sys = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.sys, ignore_errors=True)

    def panel(self, name, cur, mx):
        d = os.path.join(self.sys, "class", "backlight", name)
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "brightness"), "w").write(str(cur))
        open(os.path.join(d, "max_brightness"), "w").write(str(mx))
        return d

    def batt(self, name, **files):
        d = os.path.join(self.sys, "class", "power_supply", name)
        os.makedirs(d, exist_ok=True)
        for k, v in files.items():
            open(os.path.join(d, k), "w").write(str(v))
        return d

    def run_js(self, body):
        js = "const P = require(%s);\n(async () => { const out = {};\ntry { %s }\n" \
             "catch(e){ out.threw = String(e.message || e); }\n" \
             "process.stdout.write(JSON.stringify(out)); })();" % (json.dumps(POWER), body)
        env = dict(os.environ, PC_SYSFS=self.sys, PATH="/nonexistent:" + os.environ["PATH"])
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        return json.loads(r.stdout)

    def test_no_backlight_is_reported_not_thrown(self):
        """A desktop machine has none, and that is not a fault to show somebody."""
        out = self.run_js("out.b = P.brightness();")
        self.assertFalse(out["b"]["available"])

    def test_brightness_is_a_percentage_of_this_panel(self):
        """max_brightness is 255 on one panel and 96000 on another. A UI storing raw values gives a
        different screen on every machine."""
        self.panel("intel_backlight", 48000, 96000)
        self.assertEqual(self.run_js("out.b = P.brightness();")["b"]["percent"], 50)

    def test_a_real_panel_beats_the_acpi_shim(self):
        """A laptop can expose several backlights and some of them accept writes and change
        nothing."""
        self.panel("acpi_video0", 5, 10)
        self.panel("intel_backlight", 100, 1000)
        self.assertEqual(self.run_js("out.b = P.brightness();")["b"]["name"], "intel_backlight")

    def test_brightness_can_never_reach_zero(self):
        """On most panels 0 is OFF, not dim — and somebody who cannot see the screen cannot undo
        what they just did."""
        d = self.panel("intel_backlight", 500, 1000)
        out = self.run_js("out.r = await P.setBrightness(0);")
        self.assertGreaterEqual(out["r"]["percent"], 1)
        self.assertGreater(int(open(os.path.join(d, "brightness")).read()), 0)

    def test_brightness_is_clamped_at_the_top_too(self):
        d = self.panel("intel_backlight", 10, 1000)
        self.run_js("await P.setBrightness(500);")
        self.assertEqual(int(open(os.path.join(d, "brightness")).read()), 1000)

    def test_a_tower_has_no_battery_and_says_so(self):
        self.assertFalse(self.run_js("out.b = P.battery();")["b"]["present"])

    def test_a_battery_reporting_only_charge_is_still_a_battery(self):
        """Some report no `capacity`, only charge_now/charge_full. Refusing to compute it shows "no
        battery" on a laptop that plainly has one."""
        self.batt("BAT0", charge_now=3000, charge_full=6000, status="Discharging", type="Battery")
        b = self.run_js("out.b = P.battery();")["b"]
        self.assertTrue(b["present"])
        self.assertEqual(b["percent"], 50)
        self.assertFalse(b["charging"])

    def test_hibernate_is_refused_with_no_swap(self):
        """Offering the button on a machine with nowhere to write the image is offering a button
        that returns an error."""
        out = self.run_js("try { await P.hibernate(); out.ok = true; } catch(e){ out.threw = e.message; }")
        if not out.get("ok"):          # this box may genuinely have swap
            self.assertIn("swap", out.get("threw", ""))

    def profile_choices(self, choices, active):
        d = os.path.join(self.sys, "firmware", "acpi")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "platform_profile_choices"), "w").write(choices)
        open(os.path.join(d, "platform_profile"), "w").write(active)
        return os.path.join(d, "platform_profile")

    def test_profiles_come_from_the_kernel_not_a_daemon(self):
        """power-profiles-daemon is a wrapper around this file. Reading it directly is one less
        package to install on a machine somebody else runs."""
        self.profile_choices("low-power balanced performance", "balanced")
        p = self.run_js("out.p = P.profiles();")["p"]
        self.assertTrue(p["available"])
        self.assertEqual(p["kind"], "platform")
        self.assertEqual(p["list"], ["low-power", "balanced", "performance"])
        self.assertEqual(p["active"], "balanced")

    def test_a_profile_is_validated_against_what_this_machine_offers(self):
        """The kernel rejects an unknown value with EINVAL, which arrives as an unhelpful write
        error. Checking first means the message names the profiles that exist."""
        self.profile_choices("low-power balanced performance", "balanced")
        out = self.run_js("await P.setProfile('turbo');")
        self.assertIn("low-power", out.get("threw", ""), out)

    def test_setting_a_profile_writes_the_file(self):
        f = self.profile_choices("low-power balanced performance", "balanced")
        self.run_js("await P.setProfile('performance');")
        self.assertEqual(open(f).read().strip(), "performance")

    def test_a_machine_with_no_profiles_says_so(self):
        out = self.run_js("await P.setProfile('balanced');")
        self.assertIn("no power profiles", out.get("threw", ""))

    def test_a_governor_is_set_on_every_cpu(self):
        """A machine running one core at performance and eleven at powersave is in neither
        profile."""
        for i in range(3):
            d = os.path.join(self.sys, "devices", "system", "cpu", f"cpu{i}", "cpufreq")
            os.makedirs(d, exist_ok=True)
            open(os.path.join(d, "scaling_governor"), "w").write("powersave")
            if i == 0:
                open(os.path.join(d, "scaling_available_governors"), "w").write("performance powersave")
        self.run_js("await P.setProfile('performance');")
        for i in range(3):
            f = os.path.join(self.sys, "devices", "system", "cpu", f"cpu{i}", "cpufreq",
                             "scaling_governor")
            self.assertEqual(open(f).read().strip(), "performance", f"cpu{i} was left behind")


@unittest.skipIf(not NODE, "no node on this node")
class Audio(unittest.TestCase):
    STUB = ('#!/bin/sh\necho "wpctl $*" >> "$PC_LOG"\n'
            'case "$1" in\n'
            '  --version) echo "wpctl 0.5" ;;\n'
            '  get-volume) echo "Volume: 0.65 [MUTED]" ;;\n'
            '  status) printf "Audio\\n ├─ Sinks:\\n │      *   47. Built-in Audio [vol: 0.65]\\n'
            ' │          52. HDMI Output [vol: 1.00]\\n ├─ Sources:\\n │      *   51. Mic [vol: 0.40]\\n'
            ' ├─ Streams:\\n" ;;\n'
            'esac\nexit 0\n')

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bin = os.path.join(self.dir, "wpctl")
        open(self.bin, "w").write(self.STUB)
        os.chmod(self.bin, os.stat(self.bin).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.dir, "log")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_js(self, body):
        js = "const A = require(%s);\n(async () => { const out = {};\ntry { %s }\n" \
             "catch(e){ out.threw = String(e.message || e); }\n" \
             "process.stdout.write(JSON.stringify(out)); })();" % (json.dumps(AUDIO), body)
        env = dict(os.environ, PC_WPCTL=self.bin, PC_LOG=self.log)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        return json.loads(r.stdout)

    def argv(self):
        return open(self.log).read() if os.path.exists(self.log) else ""

    def test_a_percentage_is_sent_as_a_fraction(self):
        """`wpctl set-volume … 50` is FIVE THOUSAND PERCENT, and wpctl accepts it. On most hardware
        that is not merely loud — it is clipped, distorted, and capable of damaging a speaker."""
        self.run_js("await A.setVolume(50);")
        self.assertIn("set-volume @DEFAULT_AUDIO_SINK@ 0.500", self.argv())

    def test_it_is_clamped_at_a_documented_ceiling(self):
        self.run_js("await A.setVolume(900);")
        self.assertIn("1.500", self.argv())
        self.assertNotIn("9.000", self.argv())

    def test_nonsense_is_refused_rather_than_passed_through(self):
        out = self.run_js("await A.setVolume('loud');")
        self.assertIn("number", out.get("threw", ""))
        self.assertNotIn("set-volume", self.argv())

    def test_mute_is_a_separate_fact_from_volume(self):
        """A UI that infers muted from "volume is 0" cannot restore the level, and unmuting leaves
        silence."""
        out = self.run_js("out.s = await A.sink();")
        self.assertEqual(out["s"]["percent"], 65)
        self.assertTrue(out["s"]["muted"])

    def test_mute_is_set_explicitly_not_toggled(self):
        """A mute button and a blind toggle disagree the moment anything else changes the state —
        another app, a headset button, a second window."""
        self.run_js("await A.setMuted(true);")
        self.assertIn("set-mute @DEFAULT_AUDIO_SINK@ 1", self.argv())
        self.assertNotIn("toggle", self.argv())

    def test_the_device_list_survives_the_box_drawing(self):
        """wpctl draws a tree with box characters, and the DEFAULT device is marked with an
        asterisk — the only way to know where sound is actually going."""
        out = self.run_js("out.d = await A.devices();")
        names = [s["name"] for s in out["d"]["sinks"]]
        self.assertIn("Built-in Audio", names)
        self.assertIn("HDMI Output", names)
        default = [s for s in out["d"]["sinks"] if s["isDefault"]]
        self.assertEqual(len(default), 1)
        self.assertEqual(default[0]["id"], 47)

    def test_a_device_id_is_validated(self):
        out = self.run_js("await A.setDefault('47; reboot');")
        self.assertIn("device id", out.get("threw", ""))


if __name__ == "__main__":
    unittest.main()
