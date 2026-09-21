"""Once hibernation works, "sleep" means suspend-then-hibernate, after a delay the person chooses.

"System Settings -> Hibernation -> The default behavior should be to suspend then hibernate when
Hibernation is enabled and ready to use. The value should be configurable in Hibernation settings."

Three halves, each RUN rather than read:
  * `gentoo.sh sleep-policy <seconds>` (root, via `sudo -n` from the desktop) writes the two systemd
    drop-ins -- the sleep delay and what the lid / suspend key do -- idempotently, refuses a
    suspend-then-hibernate policy on a machine with no swap, and puts back the plain-suspend unit
    the old installer unlinked when "never hibernate" is chosen;
  * desktop/power.js READS the policy back from the same files systemd reads (main file, then the
    drop-ins, last value wins), so the panel shows what the lid will actually do -- including the
    laptop's real state today (`HibernateDelaySec=500` in sleep.conf, lid = suspend-then-hibernate
    appended to logind.conf);
  * the desktop's Sleep button follows it: `systemctl suspend-then-hibernate` when hibernation is
    ready, plain `suspend` otherwise, and plain suspend as the fallback when the kernel refuses.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENTOO = os.path.join(ROOT, "os", "gentoo.sh")
POWER = os.path.join(ROOT, "desktop", "power.js")
NODE = shutil.which("node")
BASH = shutil.which("bash")


def _shell_function(src, name):
    start = src.index("\n" + name + "() {") + 1
    end = src.index("\n}\n", start)
    return src[start:end + 2]


class Scratch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.etc = os.path.join(self.tmp, "etc-systemd")
        self.lib = os.path.join(self.tmp, "lib-systemd")
        os.makedirs(self.etc)
        os.makedirs(self.lib)
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "calls.log")
        # systemctl / sudo stubs that record their argv; `PC_FAIL_VERB` makes one systemctl verb fail.
        for name in ("systemctl", "sudo"):
            p = os.path.join(self.bin, name)
            with open(p, "w") as f:
                f.write('#!/bin/sh\necho "%s $*" >>"%s"\n'
                        '[ -n "$PC_FAIL_VERB" ] && [ "$1" = "$PC_FAIL_VERB" ] && { echo "refused" >&2; exit 1; }\n'
                        'exit 0\n' % (name, self.log))
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        self.swaps = os.path.join(self.tmp, "swaps")
        self.hibconf = os.path.join(self.tmp, "90-posterchan-hibernate.conf")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def swap(self, on=True):
        with open(self.swaps, "w") as f:
            f.write("Filename\tType\tSize\tUsed\tPriority\n")
            if on:
                f.write("/swap/swap  file  28535804  0  -2\n")

    def hibernation_configured(self, on=True):
        self.swap(on)
        with open(self.hibconf, "w") as f:
            f.write('kernel_cmdline+=" resume=UUID=a02705bc resume_offset=533760 "\n' if on else "")

    def write(self, rel, text):
        p = os.path.join(self.etc, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)

    def calls(self):
        try:
            return open(self.log).read().splitlines()
        except FileNotFoundError:
            return []

    def env(self, **extra):
        e = dict(os.environ, PATH=self.bin + ":" + os.environ["PATH"], PC_SYSTEMD_ETC=self.etc,
                 PC_SYSTEMD_LIB=self.lib, PC_PROC_SWAPS=self.swaps, PC_HIBERNATE_CONF=self.hibconf,
                 PC_GENTOO_SH=GENTOO)
        e.update(extra)
        return e

    def policy_sh(self, *args, **extra):
        body = _shell_function(open(GENTOO, encoding="utf-8").read(), "sleepPolicy")
        return subprocess.run([BASH, "-c", body + '\nsleepPolicy "$@"', "sleep-policy"] + list(args),
                              capture_output=True, text=True, env=self.env(**extra), timeout=30)

    def power(self, body, **extra):
        js = ("const P = require(%s);\n(async () => { const out = {};\n"
              "try { %s } catch(e){ out.threw = String(e.message || e); }\n"
              "process.stdout.write(JSON.stringify(out)); })();" % (json.dumps(POWER), body))
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, env=self.env(**extra), timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        return json.loads(r.stdout)


@unittest.skipIf(not BASH, "bash is required")
class TheShellWritesThePolicy(Scratch):
    def test_a_delay_writes_suspend_then_hibernate_everywhere(self):
        self.swap(True)
        r = self.policy_sh("1800")
        self.assertEqual(r.returncode, 0, r.stderr)
        sleep = open(os.path.join(self.etc, "sleep.conf.d", "90-posterchan.conf")).read()
        login = open(os.path.join(self.etc, "logind.conf.d", "90-posterchan.conf")).read()
        self.assertIn("HibernateDelaySec=1800", sleep)
        self.assertIn("AllowSuspendThenHibernate=yes", sleep)
        for key in ("HandleLidSwitch", "HandleLidSwitchExternalPower", "HandleSuspendKey"):
            self.assertIn(key + "=suspend-then-hibernate", login)
        self.assertIn("systemctl reload systemd-logind", self.calls(),
                      "logind must re-read it now, not at the next boot")

    def test_it_is_idempotent(self):
        self.swap(True)
        self.policy_sh("3600")
        first = open(os.path.join(self.etc, "logind.conf.d", "90-posterchan.conf")).read()
        self.policy_sh("3600")
        again = open(os.path.join(self.etc, "logind.conf.d", "90-posterchan.conf")).read()
        self.assertEqual(first, again)
        self.assertEqual(again.count("HandleLidSwitch="), 1, "an append stacks duplicate lines")

    def test_never_is_plain_suspend_and_restores_the_unit_the_old_installer_removed(self):
        self.swap(True)
        r = self.policy_sh("0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("HibernateDelaySec", open(os.path.join(self.etc, "sleep.conf.d", "90-posterchan.conf")).read())
        self.assertIn("HandleLidSwitch=suspend\n", open(os.path.join(self.etc, "logind.conf.d", "90-posterchan.conf")).read())
        unit = open(os.path.join(self.etc, "system", "systemd-suspend.service")).read()
        self.assertIn("ExecStart=/usr/lib/systemd/systemd-sleep suspend", unit)

    def test_the_unit_is_left_alone_where_the_package_still_has_it(self):
        open(os.path.join(self.lib, "systemd-suspend.service"), "w").write("[Unit]\n")
        self.swap(True)
        self.policy_sh("0")
        self.assertFalse(os.path.exists(os.path.join(self.etc, "system", "systemd-suspend.service")))

    def test_no_swap_refuses_hibernation_and_writes_nothing(self):
        self.swap(False)
        r = self.policy_sh("3600")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no swap", r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.etc, "logind.conf.d", "90-posterchan.conf")))

    def test_junk_is_refused(self):
        self.swap(True)
        for bad in ("1h", "-5", "abc", "99999999"):
            with self.subTest(bad=bad):
                self.assertNotEqual(self.policy_sh(bad).returncode, 0)

    def test_the_command_is_dispatched_and_hibernation_no_longer_unlinks_suspend(self):
        src = open(GENTOO, encoding="utf-8").read()
        self.assertRegex(src, r'elif \[ "\$1" = "sleep-policy" \]; then\n(?:\t#[^\n]*\n)*\tsleepPolicy "\$2"')
        hib = _shell_function(src, "hibernation")
        self.assertNotRegex(hib, r"(?m)^\s*(?:unlink|rm)\s", "plain suspend must stay possible")
        self.assertNotIn(">>/etc/systemd/logind.conf", hib, "an append on every run stacks duplicates")
        self.assertIn("sleepPolicy", hib, "enabling hibernation must set the default policy")


@unittest.skipIf(not (NODE and BASH), "node and bash are required")
class TheDesktopReadsItBack(Scratch):
    def test_not_ready_means_plain_sleep(self):
        self.hibernation_configured(False)
        out = self.power("out.p = P.sleepPolicy();")
        self.assertEqual((out["p"]["mode"], out["p"]["hibernateReady"]), ("suspend", False))

    def test_ready_with_nothing_chosen_defaults_to_suspend_then_hibernate(self):
        self.hibernation_configured(True)
        p = self.power("out.p = P.sleepPolicy();")["p"]
        self.assertEqual((p["mode"], p["delaySec"]), ("suspend-then-hibernate", 3600))
        self.assertTrue(p["canChange"])

    def test_the_laptops_real_configuration_reads_as_itself(self):
        """Measured on 192.168.0.154: the old hibernation() appended these to the MAIN files."""
        self.hibernation_configured(True)
        self.write("sleep.conf", "[Sleep]\nAllowSuspend=yes\nAllowHibernation=yes\n"
                                 "AllowSuspendThenHibernate=yes\nHibernateState=disk\n"
                                 "HibernateMode=platform\nHibernateDelaySec=500\n")
        self.write("logind.conf", "[Login]\nHandleLidSwitch=suspend-then-hibernate\n"
                                  "HandleLidSwitchExternalPower=suspend-then-hibernate\n")
        p = self.power("out.p = P.sleepPolicy();")["p"]
        self.assertEqual((p["mode"], p["delaySec"]), ("suspend-then-hibernate", 500))

    def test_what_the_shell_writes_is_what_the_desktop_reads(self):
        self.hibernation_configured(True)
        self.write("sleep.conf", "[Sleep]\nHibernateDelaySec=500\n")
        for delay, mode in (("7200", "suspend-then-hibernate"), ("0", "suspend"), ("1800", "suspend-then-hibernate")):
            with self.subTest(delay=delay):
                self.assertEqual(self.policy_sh(delay).returncode, 0)
                p = self.power("out.p = P.sleepPolicy();")["p"]
                self.assertEqual((p["mode"], p["delaySec"]), (mode, int(delay)))

    def test_the_sleep_button_follows_the_policy(self):
        self.hibernation_configured(True)
        out = self.power("out.r = await P.suspend();")
        self.assertEqual(out["r"]["mode"], "suspend-then-hibernate")
        self.assertIn("systemctl suspend-then-hibernate", self.calls())
        self.assertNotIn("systemctl suspend", self.calls())

    def test_a_refused_suspend_then_hibernate_still_sleeps(self):
        self.hibernation_configured(True)
        out = self.power("out.r = await P.suspend();", PC_FAIL_VERB="suspend-then-hibernate")
        self.assertEqual(out["r"]["mode"], "suspend")
        self.assertEqual(self.calls(), ["systemctl suspend-then-hibernate", "systemctl suspend"])

    def test_without_hibernation_the_button_is_plain_suspend(self):
        self.hibernation_configured(False)
        self.power("out.r = await P.suspend();")
        self.assertEqual(self.calls(), ["systemctl suspend"])

    def test_changing_it_goes_through_sudo_to_gentoo_sh(self):
        self.hibernation_configured(True)
        out = self.power("out.p = await P.setSleepPolicy(7200);")
        self.assertNotIn("threw", out)
        self.assertEqual(self.calls(), ["sudo -n %s sleep-policy 7200" % GENTOO])

    def test_only_the_offered_delays_are_accepted(self):
        self.hibernation_configured(True)
        for bad in (5, -1, 99999, "1h", None):
            with self.subTest(bad=bad):
                out = self.power("out.p = await P.setSleepPolicy(%s);" % json.dumps(bad))
                self.assertIn("threw", out)
        self.assertEqual(self.calls(), [], "nothing reached sudo")

    def test_an_older_gentoo_sh_is_never_run_with_an_unknown_command(self):
        """Its fallback for an unknown argument is the interactive installer MENU."""
        self.hibernation_configured(True)
        old = os.path.join(self.tmp, "gentoo-old.sh")
        with open(old, "w") as f:
            f.write('#!/bin/bash\nif [ "$1" = "hibernate" ]; then :; else menu; fi\n')
        out = self.power("out.p = await P.setSleepPolicy(3600);", PC_GENTOO_SH=old)
        self.assertIn("update", out.get("threw", ""))
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.power("out.p = P.sleepPolicy();", PC_GENTOO_SH=old)["p"]["canChange"])

    def test_spans_parse_the_way_systemd_writes_them(self):
        out = self.power("out.v = ['500','30min','1h','2h 30min','90s','1h30m','','soon','5 parsecs']"
                         ".map(P.parseSpan);")
        self.assertEqual(out["v"], [500, 1800, 3600, 9000, 90, 5400, None, None, None])


class TheWiringIsThere(unittest.TestCase):
    def test_main_preload_and_settings_carry_the_policy(self):
        main = open(os.path.join(ROOT, "desktop", "main.js"), encoding="utf-8").read()
        pre = open(os.path.join(ROOT, "desktop", "preload.js"), encoding="utf-8").read()
        ui = open(os.path.join(ROOT, "static", "js", "client", "os.js"), encoding="utf-8").read()
        self.assertRegex(main, r"ipcMain\.handle\('pc:power:sleep-policy'[^\n]*power\.setSleepPolicy")
        self.assertIn("setSleepPolicy: (seconds) => ipcRenderer.invoke('pc:power:sleep-policy'", pre)
        self.assertIn("data-sleep-delay", ui)
        self.assertIn("pcPower.setSleepPolicy", ui)


if __name__ == "__main__":
    unittest.main()
