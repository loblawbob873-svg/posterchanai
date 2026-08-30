"""A test fake must not have a contract the real module does not have.

Run: venv-unified/bin/python -m pytest tests/client/test_relay_subscription_contract.py

`Relay.subscribe` returns a subId STRING and is ended with `Relay.close(id)`. The runtime beside
this measures that against the shipped relay.js rather than asserting it from memory.

concord.js guarded both of its stop paths with `typeof sub.close === 'function'`. For a string that
is false every time, so neither the chat subscription nor the discovery subscription was EVER
closed. A leaked live subscription is not idle — the REQ stays open on every relay in the pool, and
relays cap concurrent subscriptions per connection, so after enough channel switches a freshly
opened room cannot get a live subscription at all and silently drops to the four-second poller.
Reported as "joined rooms are not reliably live", with nothing in any log.

Three Concord fixtures faked `relaySubscribe` as `() => ({close(){ …count… }})`, and the tests then
asserted on the count — `concord_live_messages_runtime.mjs` literally threw "switching channels left
the old subscription open" if the counter stayed at zero. It never did, because the fake provided
the very method the real module lacks. The suite proved the close path worked while production
leaked every subscription it ever opened. Verified by reverting the fix with the fixtures corrected:
two Concord tests go red immediately.

That is the failure this file exists to prevent, and it is not specific to Concord — it is what a
fake is FOR and what makes one dangerous. So:

  * the real contract is measured, not described;
  * every `relaySubscribe` fake in the client test tree must return a string, like the real one;
  * a file that fakes the subscribe half must fake the close half, so the author has to look at
    both; and
  * app.js's PC surface must export both. It exported `relaySubscribe` and no way to close —
    which is why concord.js could not have closed a pooled subscription even had it tried.
"""
import os
import re
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
RUNTIME = os.path.join(HERE, "relay_subscription_contract_runtime.mjs")
APP = os.path.join(ROOT, "static", "js", "client", "app.js")

HAVE_NODE = shutil.which("node") is not None


def _client_test_sources():
    out = []
    for name in sorted(os.listdir(HERE)):
        if name.endswith((".mjs", ".js")):
            p = os.path.join(HERE, name)
            with open(p, encoding="utf-8", errors="ignore") as f:
                out.append((name, f.read()))
    return out


def _code_lines(src):
    """Comment LINES dropped. Line-based on purpose: a `/* … *​/` regex run over these files pairs a
    delimiter inside a string or a regex literal with a later one and deletes live code."""
    keep = []
    for ln in src.splitlines():
        t = ln.lstrip()
        if t.startswith("//") or t.startswith("*") or t.startswith("/*"):
            continue
        keep.append(ln)
    return "\n".join(keep)


class TheRealContract(unittest.TestCase):

    @unittest.skipUnless(HAVE_NODE, "node not installed")
    def test_subscribe_returns_an_id_that_close_accepts(self):
        """Measured against the shipped relay.js — see the runtime for what it asserts."""
        r = subprocess.run(["node", RUNTIME], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + "\n" + r.stderr[-4000:])

    def test_the_pc_surface_offers_both_halves(self):
        """A subscribe with no close is not a usable API, and its absence is invisible: a module
        outside app.js simply has nothing to call, so it guesses — which is exactly what concord.js
        did, guessing an object with `.close()`."""
        # assertTrue, not assertIn: app.js is 2.5MB and a failed `assertIn` prints the whole
        # haystack, which buries the one line of diagnosis under a 3MB dump.
        src = _code_lines(open(APP, encoding="utf-8").read())
        self.assertTrue("relaySubscribe:" in src,
                        "the PC surface no longer offers relaySubscribe")
        self.assertTrue("relayClose:" in src,
                        "the PC surface offers relaySubscribe but no relayClose, so no sub-module "
                        "can end a pooled subscription — every one it opens leaks for the life of "
                        "the page, which is how the Concord rooms stopped going live")


class EveryFakeMatchesIt(unittest.TestCase):

    FAKE = re.compile(r"relaySubscribe\s*:\s*(.{0,200})", re.S)

    def _fakes(self):
        found = []
        for name, src in _client_test_sources():
            code = _code_lines(src)
            for m in self.FAKE.finditer(code):
                found.append((name, m.group(1)))
        return found

    def test_the_scan_still_finds_the_fakes(self):
        """Without this, a rename turns every assertion below into a loop over an empty list."""
        self.assertGreaterEqual(len(self._fakes()), 3,
                                "found %d relaySubscribe fakes — the scan has stopped seeing the "
                                "client test tree" % len(self._fakes()))

    def test_no_fake_returns_an_object_with_a_close_method(self):
        """The exact drift that hid the leak. The real handle is a string with no methods on it, so
        a fake answering `{close(){}}` lets code under test take a branch that can never be taken in
        a browser — and, worse, lets the test ASSERT that branch ran."""
        bad = []
        for name, body in self._fakes():
            # `({close(){…}})` / `{ close: () => …}` — an object literal carrying a close member.
            if re.search(r"\{\s*close\s*[({:]", body):
                bad.append(name)
        self.assertEqual([], sorted(set(bad)),
                         "these fixtures fake Relay.subscribe as an object with .close(), which the "
                         "real relay.js never returns: %s.\nReturn a subId string and provide a "
                         "relayClose fake instead." % ", ".join(sorted(set(bad))))

    def test_a_file_that_fakes_subscribe_also_fakes_close(self):
        """Faking half an API is how the missing half stays unnoticed for a year."""
        missing = []
        for name, src in _client_test_sources():
            code = _code_lines(src)
            if "relaySubscribe" in code and "relayClose" not in code:
                missing.append(name)
        self.assertEqual([], sorted(missing),
                         "these fixtures fake relaySubscribe but not relayClose, so nothing they "
                         "drive can be observed closing a subscription: %s" % ", ".join(sorted(missing)))


class ConcordClosesWhatItOpens(unittest.TestCase):
    """The consumer side, checked by name — concord is the only module that subscribes and the one
    the leak was found in."""

    CONCORD = os.path.join(ROOT, "static", "js", "client", "concord.js")

    def test_neither_stop_path_branches_on_a_close_method(self):
        src = _code_lines(open(self.CONCORD, encoding="utf-8").read())
        self.assertNotIn("typeof sub.close==='function'", src.replace(" ", ""),
                         "stopChatLive still tests for a .close() method the handle does not have")
        self.assertNotIn("typeofsubscription.close==='function'", src.replace(" ", ""),
                         "stopDiscovery still tests for a .close() method the handle does not have")

    def test_both_subscriptions_go_through_the_normaliser(self):
        """`subCloser` is what turns whichever of the three shapes came back into one closer. Both
        call sites must use it, or the one that does not is the one that leaks."""
        src = _code_lines(open(self.CONCORD, encoding="utf-8").read())
        self.assertIn("function subCloser(", src, "the subscription normaliser is gone")
        self.assertEqual(2, src.count("subCloser(p,p.relaySubscribe("),
                         "expected both the chat and discovery subscriptions to be normalised; "
                         "found %d" % src.count("subCloser(p,p.relaySubscribe("))


if __name__ == "__main__":
    unittest.main()
