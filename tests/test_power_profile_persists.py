"""A CHOSEN POWER PROFILE MUST SURVIVE A REBOOT.

Reported 2026-09-18: "the powersave settings need to persist after reboots, this is crazy." The
governor (`scaling_governor`) and the ACPI `platform_profile` are kernel RUNTIME state — they reset
to the boot default on every reboot — so selecting "power saver" or "performance" reverted every
time the machine came back. The idle timeout already persists (pc-idle writes a conf and re-applies
it at session start); the profile now does the same: setProfile() writes the choice to a conf, and
restoreProfile() (called from main.js's app.whenReady) replays it at boot.

RUN against a fake /sys via PC_SYSFS, exactly as the brightness/profile tests already do.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POWER = ROOT / "desktop" / "power.js"
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "no node")
class PowerProfilePersistence(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        # a fake cpufreq tree with two governors offered, currently "performance"
        self.cpu = Path(self.dir, "sys/devices/system/cpu")
        for c in ("cpu0", "cpu1"):
            d = self.cpu / c / "cpufreq"; d.mkdir(parents=True)
            (d / "scaling_available_governors").write_text("performance powersave schedutil\n")
            (d / "scaling_governor").write_text("performance\n")
        self.conf = Path(self.dir, "power-profile")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _node(self, body):
        js = ("const P = require(%s);\n(async()=>{const out={};try{%s}"
              "catch(e){out.threw=String(e.message||e);}process.stdout.write(JSON.stringify(out));})();"
              % (json.dumps(str(POWER)), body))
        env = dict(os.environ, PC_SYSFS=str(Path(self.dir, "sys")),
                   PC_POWER_PROFILE_CONF=str(self.conf))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(0, r.returncode, r.stderr[-800:])
        return json.loads(r.stdout)

    def _gov(self):
        return (self.cpu / "cpu0" / "cpufreq" / "scaling_governor").read_text().strip()

    def test_setting_a_profile_writes_the_conf(self):
        out = self._node("out.r = await P.setProfile('powersave');")
        self.assertEqual("powersave", out["r"]["active"], out)
        self.assertTrue(self.conf.exists(), "the chosen profile was not persisted to a conf")
        self.assertEqual("powersave", self.conf.read_text().strip())
        self.assertEqual("powersave", self._gov(), "the live governor was not set on all CPUs")

    def test_restore_reapplies_after_a_reboot(self):
        """Save powersave, simulate a reboot (governor back to the boot default), then restore."""
        self._node("await P.setProfile('powersave');")
        # reboot: kernel reset every CPU's governor to the boot default
        for c in ("cpu0", "cpu1"):
            (self.cpu / c / "cpufreq" / "scaling_governor").write_text("performance\n")
        out = self._node("out.r = await P.restoreProfile();")
        self.assertTrue(out["r"]["restored"], out)
        self.assertEqual("powersave", self._gov(),
                         "the saved profile was not re-applied at boot — it does not persist")

    def test_restore_with_nothing_saved_is_a_quiet_noop(self):
        out = self._node("out.r = await P.restoreProfile();")
        self.assertFalse(out["r"]["restored"])
        self.assertNotIn("threw", out, "restore threw with no saved profile instead of no-op")

    def test_restore_ignores_a_profile_the_machine_no_longer_offers(self):
        self.conf.write_text("turbo\n")   # a value not in scaling_available_governors
        out = self._node("out.r = await P.restoreProfile();")
        self.assertFalse(out["r"]["restored"])
        self.assertEqual("performance", self._gov(), "wrote an unsupported governor")

    def test_restore_does_not_rewrite_the_conf(self):
        """The replay must not re-persist — otherwise a failed apply could rewrite a good conf."""
        self._node("await P.setProfile('powersave');")
        self.conf.write_text("powersave\n")
        before = self.conf.stat().st_mtime_ns
        self._node("await P.restoreProfile();")   # already active -> no-op, must not touch the conf
        self.assertEqual(before, self.conf.stat().st_mtime_ns,
                         "restoreProfile rewrote the conf on replay")


if __name__ == "__main__":
    unittest.main()
