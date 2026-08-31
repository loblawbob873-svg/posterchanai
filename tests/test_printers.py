"""Adding a printer must not require a password that this OS deliberately does not issue.

"how are users supposed to install a printer on this OS? You need username and password access for
cups" — and the answer was that they cannot. CUPS is installed and running on every PosterChanOS
machine and its admin pages are the only way in; they authenticate a system account through PAM, and
`pc-provision-user` says why that can never succeed here:

    Identity accounts deliberately have no Unix password: authentication happened at the Nostr
    sign-in screen.

So the one account on the machine has nothing to type, and 127.0.0.1:631/admin refuses it for ever.
Putting the user in `lp`/`lpadmin` was necessary and not sufficient — measured on real hardware,
where /admin answered 200 only because that particular account happened to have a password set by
hand.

The fix is not to invent a password. That account already holds `NOPASSWD: ALL` sudo, so the shell
runs the CUPS command-line tools directly, exactly as Displays and Power drive their hardware.

WHAT THESE TESTS ARE FOR. The parsers, because `lpstat` output is the only contract between CUPS and
this panel and it is text; and the argument handling, because a printer name and a device URI both
arrive from the NETWORK. `lpadmin -p "$name"` through a shell would be a remote device advertising
itself into a command line, so every call is a fixed argv and the name is validated here, on the
privileged side, rather than trusted from the page.
"""
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MOD = ROOT / "desktop" / "printers.js"


def _node(expr):
    out = subprocess.run(["node", "-e",
                          "const p=require('./desktop/printers');" + expr],
                         cwd=ROOT, capture_output=True, text=True, timeout=60)
    if out.returncode:
        raise AssertionError(out.stderr[-2000:])
    return json.loads(out.stdout.strip().splitlines()[-1])


class TheParsersReadRealCupsOutput(unittest.TestCase):
    def test_devices_come_off_lpstat_v(self):
        got = _node("""
          const out = 'device for Brother_HL: ipp://192.168.0.9:631/ipp/print\\n'
                    + 'device for hall: usb://Canon/LBP?serial=A1\\n';
          process.stdout.write(JSON.stringify(p._parseDevices(out)));""")
        self.assertEqual([{"name": "Brother_HL", "uri": "ipp://192.168.0.9:631/ipp/print"},
                          {"name": "hall", "uri": "usb://Canon/LBP?serial=A1"}], got)

    def test_state_comes_off_lpstat_p(self):
        got = _node("""
          const out = 'printer hall is idle.  enabled since Sun 31 Aug 2026\\n'
                    + 'printer old is disabled since Fri 29 Aug 2026 -\\n';
          process.stdout.write(JSON.stringify(p._parseState(out)));""")
        self.assertEqual({"hall": "idle", "old": "disabled"}, got)

    def test_a_machine_with_no_printers_parses_to_nothing_rather_than_throwing(self):
        got = _node("process.stdout.write(JSON.stringify("
                    "[p._parseDevices(''), p._parseState('lpstat: No destinations added.')]));")
        self.assertEqual([[], {}], got)


class NetworkSuppliedNamesCannotBecomeCommands(unittest.TestCase):
    # ASSERT THE REASON, NOT JUST THE FAILURE. The first version of these checked only `ok === false`
    # and PASSED with the validation deleted: with no `sudo` on the build host the command fails
    # anyway, so "refused" and "tried it and could not" were indistinguishable. A test that passes
    # against the broken code is worse than no test. Each case now requires the VALIDATION message,
    # which only appears when the input was rejected before anything ran.
    def test_a_name_with_a_shell_metacharacter_is_refused(self):
        for bad in ("hall; rm -rf /", "a b", "../etc/passwd", "$(id)", "`id`", "", "x" * 200):
            with self.subTest(name=bad):
                got = _node("p.add({name:%s,uri:'ipp://x/y'}).then(r=>"
                            "process.stdout.write(JSON.stringify(r)));" % json.dumps(bad))
                self.assertFalse(got["ok"], "%r was accepted as a printer name" % bad)
                self.assertIn("printer name may use", got["error"],
                              "%r was not REJECTED — it was attempted and happened to fail, which "
                              "on a machine with working sudo would have run it" % bad)

    def test_a_uri_that_is_not_a_uri_is_refused(self):
        for bad in ("not-a-uri", "/etc/passwd", "; reboot"):
            with self.subTest(uri=bad):
                got = _node("p.add({name:'ok',uri:%s}).then(r=>"
                            "process.stdout.write(JSON.stringify(r)));" % json.dumps(bad))
                self.assertFalse(got["ok"])
                self.assertIn("not a device URI", got["error"],
                              "%r reached lpadmin instead of being rejected" % bad)

    def test_every_privileged_call_validates_before_running_anything(self):
        """setDefault/remove/testPage take a name straight from a row; each must check it too."""
        for fn in ("setDefault", "remove", "testPage"):
            with self.subTest(fn=fn):
                got = _node("p.%s('a; reboot').then(r=>"
                            "process.stdout.write(JSON.stringify(r)));" % fn)
                self.assertFalse(got["ok"], "%s ran with an unvalidated name" % fn)
                self.assertEqual("unknown printer", got["error"],
                                 "%s attempted the command instead of rejecting the name" % fn)

    def test_nothing_is_ever_passed_through_a_shell(self):
        # THE IMPORT, not a substring. An earlier version stripped "execFile(" and looked for
        # "exec(" in what was left — which matched every REGEX `.exec(line)` in the parsers and
        # failed against code that was correct. What actually matters is which child_process
        # function is imported: `exec` runs a shell, `execFile` does not.
        src = MOD.read_text(encoding="utf-8")
        imported = re.search(r"require\('child_process'\)", src)
        self.assertIsNotNone(imported, "printers.js no longer spawns anything — has it moved?")
        decl = src[:imported.start()].rsplit("const", 1)[-1]
        self.assertIn("execFile", decl, "printers.js must import execFile")
        self.assertNotRegex(decl, r"\bexec\b(?!File)",
                            "printers.js imports child_process.exec, which runs a SHELL — a device "
                            "URI and a printer name both arrive from the network")
        self.assertNotIn("shell: true", src, "a shell was re-enabled on the spawn options")


class TheUiIsWiredToIt(unittest.TestCase):
    def test_the_bridge_is_exposed_and_handled(self):
        pre = (ROOT / "desktop" / "preload.js").read_text(encoding="utf-8")
        main = (ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
        self.assertIn("exposeInMainWorld('pcPrinters'", pre)
        for ch in ("status", "discover", "add", "default", "remove", "test"):
            self.assertIn("ipcMain.handle('pc:printers:%s'" % ch, main,
                          "pc:printers:%s is exposed to the page but nothing handles it" % ch)

    def test_settings_has_a_printers_page_gated_on_the_bridge(self):
        os_js = (ROOT / "static" / "js" / "client" / "os.js").read_text(encoding="utf-8")
        self.assertIn('data-settings-page="printers"', os_js)
        self.assertIn("window.pcPrinters?", os_js,
                      "the page must be gated on the bridge, or a browser shows a panel whose "
                      "every button does nothing")
        self.assertIn("data-printer-find", os_js)
        self.assertIn("pcPrinters.testPage", os_js,
                      "there is no way to prove the printer actually prints")


if __name__ == "__main__":
    unittest.main()


class TheDriverIsChosenByTransport(unittest.TestCase):
    """Hardcoding `-m everywhere` failed on the first real printer it ever met.

    IPP Everywhere is the right default for a modern network printer and needs no PPD on disk — but
    lpadmin refuses it for anything that is not an IPP connection: "IPP Everywhere driver requires
    an IPP connection". Measured against a Brother on a real network, which CUPS discovers as
    `lpd://brw…/BINARY_P1`: every add failed.

    NINE UNIT TESTS PASSED THROUGHOUT, because every one of them exercised a REJECTION — a bad name,
    a bad URI — and nothing had ever added a printer. The bug lived in the only path they did not
    take, which is the path a person takes.

    These drive the real `add()` with a fake sudo that records its argv, so the model actually
    chosen is observable without a printer.
    """

    def _argv_for(self, uri):
        """Run add() against a fake sudo that writes its arguments out."""
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "argv"
            fake = Path(td) / "sudo"
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$0.log"\nexit 0\n')
            fake.chmod(0o755)
            out = subprocess.run(
                ["node", "-e",
                 "const p=require('./desktop/printers');"
                 "p.add({name:'q',uri:process.argv[1]}).then(r=>"
                 "process.stdout.write(JSON.stringify(r)));", uri],
                cwd=ROOT, capture_output=True, text=True, timeout=60,
                env={**os.environ, "PC_SUDO": str(fake)})
            assert out.returncode == 0, out.stderr[-800:]
            recorded = Path(str(fake) + ".log")
            return recorded.read_text().split("\n") if recorded.exists() else []

    def test_an_ipp_printer_gets_the_driverless_model(self):
        argv = self._argv_for("ipp://192.168.0.9:631/ipp/print")
        self.assertIn("everywhere", argv,
                      "an IPP printer should use IPP Everywhere and needs no PPD: %s" % argv)

    def test_an_lpd_printer_is_not_given_an_ipp_only_driver(self):
        argv = self._argv_for("lpd://brw44f79f077cca/BINARY_P1")
        self.assertNotIn("everywhere", argv,
                         "an LPD printer was given the IPP Everywhere driver, which lpadmin refuses "
                         "outright — this is the shape that failed on real hardware: %s" % argv)

    def test_a_socket_printer_is_not_given_an_ipp_only_driver(self):
        argv = self._argv_for("socket://192.168.0.50:9100")
        self.assertNotIn("everywhere", argv, argv)

    def test_the_printer_is_still_enabled_whichever_driver_is_used(self):
        """`-E` is what makes the queue accept jobs; a queue added without it looks installed and
        silently holds everything."""
        for uri in ("ipp://x/ipp/print", "lpd://y/queue"):
            with self.subTest(uri=uri):
                self.assertIn("-E", self._argv_for(uri))
