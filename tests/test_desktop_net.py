"""Wifi and network for the PosterChan shell, RUN against a stub NetworkManager.

The OS build already installs net-misc/networkmanager, so the shell does not need to manage radios
itself — it needs to speak to the thing that does. nmcli's terse mode is a documented contract
NetworkManager treats as an API, and stubbing ONE executable is what lets all of this be tested on a
box with no wifi hardware, no system bus and no NetworkManager at all.

Two of these are the reason this file exists rather than a wrapper somebody eyeballed:

  * SSIDs contain colons and the field separator IS a colon. nmcli escapes them as `\\:`; a plain
    split tears "Cafe: Free" into two fields and shifts every column after it, so the security
    column becomes part of the name and the row is quietly WRONG rather than obviously broken.
  * A password passed as an argument is in the process table, readable by every other user on the
    machine via `ps` for as long as the connect takes.
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NET = os.path.join(ROOT, "desktop", "net.js")
NODE = shutil.which("node") or shutil.which("nodejs")

# A stub nmcli. It records how it was called (argv AND stdin) and answers the terse format for real.
STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$PC_NMCLI_LOG"
case "$1 $2 $3" in
  "--version  ") echo "nmcli tool, version 1.48.0" ;;
esac
case "$*" in
  *"device status"*)
    printf 'wlan0:wifi:connected:Cafe\\: Free\n'
    printf 'eth0:ethernet:unavailable:--\n'
    printf 'lo:loopback:unmanaged:--\n' ;;
  *"device wifi list"*)
    # The same network on two bands and from two mesh nodes — one name, four rows.
    printf '*:Cafe\\: Free:71:WPA2:5180 MHz\n'
    printf ' :Cafe\\: Free:44:WPA2:2437 MHz\n'
    printf ' :Neighbour:88:WPA2:2412 MHz\n'
    printf ' :Neighbour:52:WPA2:5240 MHz\n'
    printf ' :OpenGuest:30:--:2462 MHz\n'
    printf ' ::19:WPA2:2412 MHz\n' ;;
  *"connection show"*)
    printf 'Cafe\\: Free:802-11-wireless:wlan0\n'
    printf 'Wired:802-3-ethernet:--\n' ;;
  *"wifi connect BadPassword"*)
    echo "Error: 802-11-wireless-security.psk: property is invalid." >&2; exit 4 ;;
esac
# stdin is drained so a --ask connect does not block; recorded so the test can prove where the
# secret actually went.
if [ -n "$PC_NMCLI_STDIN" ]; then cat >> "$PC_NMCLI_STDIN"; fi
exit 0
"""


@unittest.skipIf(not NODE, "no node on this node")
class NmcliClient(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.bin = os.path.join(self.dir, "nmcli")
        with open(self.bin, "w") as fh:
            fh.write(STUB)
        os.chmod(self.bin, os.stat(self.bin).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.dir, "argv.log")
        self.stdin = os.path.join(self.dir, "stdin.log")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_js(self, script):
        js = "const N = require(%s);\n(async () => { const out = {};\ntry { %s }\n" \
             "catch(e){ out.threw = String(e.message || e); }\n" \
             "process.stdout.write(JSON.stringify(out)); })();" % (json.dumps(NET), script)
        env = dict(os.environ, PC_NMCLI=self.bin, PC_NMCLI_LOG=self.log, PC_NMCLI_STDIN=self.stdin)
        r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout)

    def _argv(self):
        return open(self.log).read() if os.path.exists(self.log) else ""

    def test_an_ssid_containing_a_colon_survives_the_parser(self):
        out = self.run_js("out.d = await N.devices();")
        wlan = [d for d in out["d"] if d["device"] == "wlan0"][0]
        self.assertEqual(wlan["connection"], "Cafe: Free",
                         "the escaped colon split the row — every column after the name is now "
                         "shifted, and nothing about that looks like an error")
        self.assertEqual(wlan["state"], "connected")

    def test_one_network_is_one_row(self):
        """A band-steering router publishes the same name on 2.4 and 5 GHz and a mesh publishes it
        from every node. Shown raw, one network appears four times — which reads as four networks to
        anybody who is not a network engineer."""
        out = self.run_js("out.w = await N.wifi(false);")
        names = [n["ssid"] for n in out["w"]]
        self.assertEqual(len(names), len(set(names)), names)
        self.assertNotIn("", names, "a hidden network with no name was offered as something to join")
        best = {n["ssid"]: n for n in out["w"]}
        self.assertEqual(best["Neighbour"]["signal"], 88, "the weaker radio won the dedupe")

    def test_the_connected_network_sorts_first_and_says_so(self):
        out = self.run_js("out.w = await N.wifi(false);")
        self.assertEqual(out["w"][0]["ssid"], "Cafe: Free")
        self.assertTrue(out["w"][0]["active"])

    def test_an_open_network_is_marked_open(self):
        out = self.run_js("out.w = await N.wifi(false);")
        opens = [n for n in out["w"] if not n["secure"]]
        self.assertEqual([n["ssid"] for n in opens], ["OpenGuest"])

    def test_a_password_never_reaches_the_process_table(self):
        """`nmcli ... password <secret>` is readable by every other user on the machine through `ps`
        for as long as the connect takes."""
        self.run_js("out.c = await N.connect('Neighbour', 'hunter2');")
        self.assertNotIn("hunter2", self._argv(),
                         "the wifi password was passed as a command-line argument")
        self.assertIn("--ask", self._argv(), "no --ask, so nmcli was not expecting it on stdin")
        self.assertIn("hunter2", open(self.stdin).read(),
                      "the password did not reach nmcli at all — the connect would just hang")

    def test_a_known_network_is_joined_without_asking_again(self):
        """`device wifi connect` with no password only sometimes re-uses the stored secret;
        `connection up` always does. Getting this wrong is a shell that asks for a password it is
        already holding."""
        out = self.run_js("out.c = await N.connect('Cafe: Free');")
        self.assertTrue(out["c"]["reused"], out)
        self.assertIn("connection up id Cafe: Free", self._argv())

    def test_a_refusal_does_not_kill_the_shell(self):
        """nmcli can exit BEFORE it reads stdin — a rejected password is exactly that, it refuses on
        the arguments alone. Writing the secret then raises EPIPE asynchronously on the stream, and
        an 'error' event with no listener is re-thrown by Node and takes the whole process down. So
        the desktop shell died on a wrong wifi password. This showed up as one failure in a full
        parallel suite run and passed every time it was run alone, which is what a timing-dependent
        crash looks like from the outside."""
        for _ in range(6):
            out = self.run_js("await N.connect('BadPassword', 'nope');")
            self.assertIn("threw", out, "the failure was swallowed")

    def test_a_refusal_says_which_refusal_it_was(self):
        """"wrong password", "no such network" and "the radio is off" need different answers from a
        person, and an exit code tells them apart from none of the others."""
        out = self.run_js("await N.connect('BadPassword', 'nope');")
        self.assertIn("psk", out.get("threw", ""), out)

    def test_forget_removes_the_saved_profile(self):
        self.run_js("await N.forget('Cafe: Free');")
        self.assertIn("connection delete id Cafe: Free", self._argv())

    def test_status_answers_what_the_corner_of_the_screen_needs(self):
        out = self.run_js("out.s = await N.status();")
        s = out["s"]
        self.assertTrue(s["online"])
        self.assertEqual(s["kind"], "wifi")
        self.assertEqual(s["name"], "Cafe: Free")
        self.assertEqual(s["signal"], 71)
        self.assertNotIn("lo", [d["device"] for d in s["devices"] if d["state"] == "connected"])

    def test_the_escape_helper_handles_a_backslash_too(self):
        out = self.run_js(r"out.f = N.fields('a\\\\b:c\\:d:e');")
        self.assertEqual(out["f"], ["a\\b", "c:d", "e"])


if __name__ == "__main__":
    unittest.main()
