"""Puts ext_gate_sim.js - the NIP-07 gate's own suite - INTO the suite.

pytest collects .py files, so `node ext_gate_sim.js` passing on a laptop would mean nothing about
any deploy. This is the wrapper every other sim here has.

The property it defends is the one the user kept hitting: signing in restores everything sealed to
your own key at once, and an extension that caps approval windows DENIES THE REST WITHOUT PROMPTING.
The sim runs the SHIPPED gate against that behaviour.
"""
import os
import re
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SIM = os.path.join(ROOT, "tests", "client", "ext_gate_sim.js")
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "no node on this node")
class ExtensionGateSim(unittest.TestCase):
    def test_the_gate_suite_passes(self):
        r = subprocess.run([NODE, SIM], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, (r.stdout + r.stderr)[-3000:])
        self.assertIn("the gate holds", r.stdout)

    def test_the_sim_reproduces_the_bug_it_guards_against(self):
        """A guard that cannot demonstrate the failure is a guard nobody can trust. The sim runs the
        SAME fan-out with no gate and asserts it loses documents to silent denials."""
        r = subprocess.run([NODE, SIM], capture_output=True, text=True, timeout=120)
        self.assertIn("reproduces: most documents never arrive", r.stdout)
        self.assertIn("reproduces: refusals with no prompt", r.stdout)


class EveryExtensionCallGoesThroughTheGate(unittest.TestCase):
    """The gate is worth nothing if one call site still reaches window.nostr directly.

    Read off the nip07 branch of the signer factory, which is the only place that is allowed to
    name window.nostr at all."""

    @classmethod
    def setUpClass(cls):
        src = open(APP, encoding="utf-8").read()
        i = src.index("if (mode === 'nip07'){")
        cls.branch = src[i:src.index("if (mode === 'nip46')", i)]
        cls.app = src

    def _pairs(self):
        """(key, method) for every extension call in the branch. Read off COLLAPSED source: the
        calls wrap across lines, and a line-based check reports the continuation as unqueued."""
        flat = re.sub(r"\s+", " ", self.branch)
        return re.findall(r"X\.call\(\s*(null|[^,]+?)\s*,\s*\(\)\s*=>\s*window\.nostr\.([A-Za-z0-9_.]+)",
                          flat)

    def test_the_branch_routes_everything_through_it(self):
        """One call site still reaching window.nostr directly is the whole bug back again."""
        flat = re.sub(r"\s+", " ", self.branch)
        # The capability probe is the one allowed bare mention: it reads a property, asks nothing.
        flat = flat.replace("window.nostr && window.nostr.nip44", "")
        gated = {m for _, m in self._pairs()}
        seen = set(re.findall(r"window\.nostr\.([A-Za-z0-9_.]+)\s*\(", flat))
        self.assertEqual(seen - gated, set(),
                         f"these reach the extension unqueued: {sorted(seen - gated)}")
        self.assertTrue(gated, "nothing goes through the gate at all")

    def test_decrypts_are_coalesced_and_signing_is_not(self):
        """A decrypt is a pure function of (peer, ciphertext), so identical asks share one answer.
        Signing must not: two identical-looking requests are two events that each need a signature.
        Encrypting must not either - it is randomised, so a shared answer would be wrong."""
        pairs = dict((m, k) for k, m in self._pairs())
        for meth in ("nip04.decrypt", "nip44.decrypt"):
            self.assertIn(meth, pairs, f"{meth} no longer goes through the gate")
            self.assertNotEqual(pairs[meth], "null", f"{meth} is not coalesced")
        for meth, key in pairs.items():
            if "encrypt" in meth or "signEvent" in meth:
                self.assertEqual(key, "null",
                                 f"{meth} is coalesced; identical requests would share one answer")

    def test_the_bound_sits_above_the_extension_s_own_prompt_timeout(self):
        """The extension waits 115s for a human and then denies. A shorter bound here would cut off
        somebody who was simply reading the prompt."""
        ext = open(os.path.join(ROOT, "extension", "background.js"), encoding="utf-8").read()
        self.assertIn("115000", ext, "the extension's prompt timeout moved - re-read this test")
        i = self.app.index("const _extGate = {")
        block = self.app[i:i + 900]
        ms = int(next(p for p in block.split("_MS:")[1].split(",")[0].split()))
        self.assertGreater(ms, 115000, "the page gives up before the extension's own prompt does")


if __name__ == "__main__":
    unittest.main()
