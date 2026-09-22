"""A REMOTE SIGNER THAT CHANGES ACCOUNT MUST NOT SIGN FOR THIS SESSION IN SILENCE.

Reported 2026-09-18, by somebody trying to log a TV in with a second person's key:

    "I tried on my phone but I don't think the signer worked, my other account didn't work on
     desktop until I switched back"

Which is exactly what the code did. The NIP-46 client asks the signer for its pubkey ONCE, at connect
time, and caches it — right, because a round trip per signature would be absurd. But the signer is a
whole separate device with its own account switcher. A phone signing for a desktop, then switched to
a second account, keeps answering every request, with a different key. The desktop's session still
believes it is the first account; the events come back signed by the second; nothing compares them.
What the user sees is a machine that has simply stopped working, with nothing on either screen.

THE SIGNED EVENT CARRIES THE PUBKEY THAT SIGNED IT, so the check costs nothing and needs no extra
request. Refusing is the right outcome rather than a warning: publishing would post from an identity
the user did not choose, under a session that still thinks it is someone else.

The switcher's own note already said the app can only state which key it EXPECTS ("the extension or
the remote signer decides which key it hands back"). This is the other half — noticing when the
expectation is broken.
"""
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from tests.client_source import client_source

ROOT = Path(__file__).resolve().parents[2]
APP = client_source()


def _sign_event_fn() -> str:
    """The shipped signEvent, sliced out of the NIP-46 client.

    THERE ARE TWO of them — the bunker:// client (`this._req`, a request/response object) and the
    nostrconnect client this fix is in (`this._send`, positional params). Taking the first one by
    index ran the wrong function under the harness and reported `this._req is not a function`, which
    reads exactly like a broken test. The anchor is the TRANSPORT, not the comment above it, so the
    slice still finds the right function when the fix is removed to prove this test can fail.
    """
    found = []
    i = APP.find("    async signEvent(tpl){")
    while i >= 0:
        j = APP.index("    nip04enc(peer, text)", i)
        found.append(APP[i:j])
        i = APP.find("    async signEvent(tpl){", j)
    picked = [f for f in found if "this._send(" in f]
    assert len(picked) == 1, f"expected one _send-based signEvent, found {len(picked)} of {len(found)}"
    return picked[0]


class SignerAccountSwitch(unittest.TestCase):
    def run_sign(self, expected, returned):
        """Run the shipped signEvent under node with a stubbed transport."""
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node is not installed")
        body = _sign_event_fn().strip().rstrip(",")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp, "s.mjs")
            script.write_text(
                "const log = { toast: null };\n"
                "globalThis.toast = m => { log.toast = String(m); };\n"
                f"const returned = {json.dumps(returned)};\n"
                "const obj = {\n"
                f"  userPk: {json.dumps(expected)},\n"
                "  _send: async () => JSON.stringify(returned),\n"
                f"  {body}\n"
                "};\n"
                "const out = { ...log };\n"
                "try {\n"
                "  const ev = await obj.signEvent({ kind: 1, content: 'x' });\n"
                "  out.ok = true; out.pubkey = ev.pubkey;\n"
                "} catch (e) {\n"
                "  out.ok = false; out.err = String(e && e.message || e);\n"
                "  out.mismatch = e && e.signerPubkeyMismatch || null;\n"
                "}\n"
                "out.toast = log.toast;\n"
                "process.stdout.write(JSON.stringify(out));\n",
                encoding="utf-8")
            r = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=60)
            self.assertEqual(0, r.returncode, r.stderr[-500:])
            return json.loads(r.stdout or "{}")

    def test_a_signature_from_another_account_is_refused(self):
        """THE REPORTED CASE: the phone switched, so the key coming back is not this session's."""
        a, b = "a" * 64, "b" * 64
        out = self.run_sign(expected=a, returned={"kind": 1, "pubkey": b, "sig": "00"})
        self.assertFalse(out["ok"], "the desktop accepted an event signed by a different account")
        self.assertTrue(out.get("mismatch"), "the refusal does not say which keys disagreed")
        self.assertEqual(a, out["mismatch"]["expected"])
        self.assertEqual(b, out["mismatch"]["got"])

    def test_it_says_so_on_screen(self):
        """Silence is the actual defect — a machine that 'stopped working' with nothing said."""
        out = self.run_sign(expected="a" * 64, returned={"kind": 1, "pubkey": "b" * 64})
        self.assertTrue(out.get("toast"), "nothing was shown to the person using the machine")
        self.assertRegex(out["toast"].lower(), r"different account|switch",
                         "the message does not name the cause or the way out")

    def test_the_ordinary_case_is_untouched(self):
        """Same key: signing must be exactly as before, no extra request, no warning."""
        a = "a" * 64
        out = self.run_sign(expected=a, returned={"kind": 1, "pubkey": a, "sig": "00"})
        self.assertTrue(out["ok"], out.get("err"))
        self.assertEqual(a, out["pubkey"])
        self.assertIsNone(out.get("toast"), "a normal signature warned about nothing")

    def test_a_session_with_no_expectation_yet_still_signs(self):
        """Before connect has answered, `userPk` is empty; that is not a mismatch."""
        out = self.run_sign(expected="", returned={"kind": 1, "pubkey": "b" * 64, "sig": "00"})
        self.assertTrue(out["ok"], "an unconnected session refused its own first signature")


if __name__ == "__main__":
    unittest.main()
