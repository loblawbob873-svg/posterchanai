"""Changing your relays must not lose Notes, Passwords or Budget.

They live nowhere but a relay. Point the app at a different one and they are simply not there: the
screens read the pool they are connected to, so the vault reads EMPTY and the notebook reads empty,
and the first save then writes a fresh version to the new relay while every earlier one stays on the
old — one library split across two relays, each device seeing whichever half it can reach. Nothing
warns, because from the app's side nothing is wrong: it asked a relay for events and the relay
honestly had none.

The events are already on the device, signed. A Nostr event is self-authenticating, so carrying one
over is a byte copy.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")


def _src():
    with open(APP, encoding="utf-8") as f:
        return f.read()


class TopLevelBindings(unittest.TestCase):
    """The two mistakes that made the whole client a blank page, caught by name.

    `PC.carryPrivateToRelays = …` at app.js top level threw `PC is not defined` on script
    evaluation, so everything after it — including the DOMContentLoaded boot on the last line — never
    ran. Web, PWA, APK and Electron, all blank. `PC` is a sub-module convention (`PC = window.__PC`
    in notes.js/vault.js/budget.js); app.js is the file that BUILDS `window.__PC` and has no such
    binding. `ME` is the same shape of error in reverse: an object here, a function in the modules.

    Both are invisible to a source-text assertion about the feature, which is why they shipped past
    eight passing tests.
    """

    def test_app_js_never_uses_the_sub_module_PC_binding(self):
        """`PC.` in app.js is a ReferenceError — the bridge it BUILDS is window.__PC.

        Comment lines are skipped, and that is not a softening: the three hits this had were all
        prose EXPLAINING the bug (one of them the note left when `PC.syncBlobs.CHUNK` threw on the
        first chunked upload), so the test was red for describing what it exists to prevent. A test
        that cannot be made green without deleting the explanation is one people turn off.
        """
        s = _src()
        hits = []
        for i, ln in enumerate(s.splitlines()):
            t = ln.strip()
            if t.startswith("//") or t.startswith("*") or t.startswith("/*"):
                continue
            if re.search(r"(?<![\w.$])PC\.", ln):
                hits.append((i + 1, t))
        self.assertEqual(hits, [], "app.js has no `PC`; the bridge it builds is window.__PC")

    def test_app_js_never_calls_ME_as_a_function(self):
        s = _src()
        hits = [(i + 1, ln.strip()) for i, ln in enumerate(s.splitlines())
                if re.search(r"(?<![\w.$])ME\(\)", ln) and "get ME()" not in ln]
        self.assertEqual(hits, [], "`ME` is an object in app.js — ME() is the sub-module idiom")

    def test_the_export_hangs_off_the_bridge_that_exists(self):
        self.assertIn("window.__PC = {\n    // Republish the encrypted libraries", _src())


class CarryOnRelayChange(unittest.TestCase):
    def test_a_relay_change_flags_the_carry(self):
        s = _src()
        blk = s[s.index("if($('#set-relays-on')){"):s.index("if($('input[name=media-mode]'))")]
        self.assertIn("localStorage.setItem(_CARRY_KEY", blk,
                      "changing relays must schedule the copy; the reload happens either way")
        self.assertIn("needReload=true", blk)

    def test_it_runs_on_reconnect_not_on_every_boot(self):
        s = _src()
        # The BRANCH, not the exact line: things legitimately join it (the notes/vault queue drain
        # did), and pinning the punctuation makes this go red for a change that is not a regression.
        i = s.index("if(s === 'ok'){")
        branch = s[i:s.index("}", i)]
        self.assertIn("_carryIfRelaysChanged()", branch,
                      "the carry no longer runs when the relay comes back")
        self.assertIn("_flushOutbox()", branch)
        body = s[s.index("async function _carryIfRelaysChanged"):]
        body = body[:body.index("\n  }")]
        self.assertIn("localStorage.getItem(_CARRY_KEY)", body)
        self.assertIn("if(!flag) return;", body)

    def test_a_partial_copy_is_retried_not_forgotten(self):
        """One relay down mid-run leaves half a library on the new relay and looks finished."""
        s = _src()
        body = s[s.index("async function _carryIfRelaysChanged"):]
        body = body[:body.index("\n  }")]
        self.assertIn("r.moved === r.total", body)
        self.assertIn("removeItem(_CARRY_KEY)", body)
        self.assertNotIn("removeItem(_CARRY_KEY); }\n    const r", body)

    def test_only_the_private_libraries_are_carried(self):
        """Public posts are already on the network; re-broadcasting a year of them is a different
        act with different consequences."""
        s = _src()
        d = s[s.index("const _CARRY_D = ["):]
        d = d[:d.index("];")]
        for ns in ("pcai:note:", "pcai:pw:", "pcai:pwkey", "pcai:budget"):
            self.assertIn(ns, d, "%s has no other copy and must be carried" % ns)
        # Signed by the SERVER's per-user storage key, so `authors:[me]` could never match them —
        # and they live on the server's own relay, which a client relay change does not touch.
        for ns in ("files-index", "drafts"):
            self.assertNotIn(ns, d, "%s is not this client's to carry" % ns)
        # Published as PLAINTEXT. Copying them hands a full subscription list and reading history to
        # a relay the user may have just added on someone else's recommendation.
        for ns in ("news-feeds", "news-read"):
            self.assertNotIn(ns, d, "%s is not ciphertext; moving it is a different act" % ns)
        body = s[s.index("async function carryPrivateToRelays"):s.index("async function _carryIfRelaysChanged")]
        self.assertIn("kinds:[30078]", body, "only the datastore kind, not the timeline")
        self.assertIn("authors:[ME.pubkey]", body, "another author's events are not ours to move")

    def test_the_events_are_republished_as_signed_not_rebuilt(self):
        """No key is involved. A rebuilt event would be a new id and a new created_at, which loses
        the replaceable ordering that decides which version wins."""
        body = _src()
        body = body[body.index("async function carryPrivateToRelays"):body.index("async function _carryIfRelaysChanged")]
        self.assertIn("Relay.publishTo([u], ev", body)
        self.assertNotIn("finalizeEvent", body)
        self.assertNotIn("nip44enc", body)

    def test_a_second_device_can_still_carry(self):
        """A device that has only READ the library holds none of it: budget.js and news.js absorb
        relay results into their own state and never call Store.saveEvent. Sourcing the copy from
        the cache alone would copy nothing, report success, and leave the library behind."""
        s = _src()
        self.assertIn("async function stashPrivateBeforeRelayChange", s)
        self.assertIn("await stashPrivateBeforeRelayChange()", s)
        body = s[s.index("async function stashPrivateBeforeRelayChange"):s.index("async function carryPrivateToRelays")]
        self.assertIn("Relay.query(", body), "it must read the OLD relays, while still on them"
        self.assertIn("Store.saveEvent(ev)", body)

    def test_the_budget_doc_survives_cache_eviction(self):
        """Unpinned, it is evicted at 4500 events — and then there is nothing to carry."""
        store = os.path.join(ROOT, "static", "js", "client", "store.js")
        with open(store, encoding="utf-8") as f:
            self.assertIn("t[1] === 'pcai:budget'", f.read())

    def test_completion_means_every_relay_took_it(self):
        """publish() resolves on the FIRST relay that accepts. When someone ADDS a relay, the old
        one accepts every republish instantly — "moved" would read 100% while the new relay, the
        only one that needed anything, got nothing."""
        body = _src()
        body = body[body.index("async function carryPrivateToRelays"):body.index("async function _carryIfRelaysChanged")]
        self.assertIn("Relay.publishTo([u], ev", body)
        self.assertIn("if(ok >= urls.length) moved++;", body)
        self.assertNotIn("Relay.publish(ev", body)

    def test_the_flag_survives_every_outcome_but_success(self):
        """A concurrent run, guest mode at cold start, offline — each looked like "nothing to do"."""
        body = _src()
        body = body[body.index("async function _carryIfRelaysChanged"):]
        body = body[:body.index("\n  }")]
        self.assertIn("if(r.busy || r.noUser || r.offline) return;", body)
        self.assertIn("if(r.moved === r.total)", body)
        self.assertIn("Relay.ready", body, "publishing into a half-open pool drops silently")

    def test_it_is_paced(self):
        """A relay that rate-limits a burst leaves a PARTIAL copy — the outcome that looks finished."""
        body = _src()
        body = body[body.index("async function carryPrivateToRelays"):body.index("async function _carryIfRelaysChanged")]
        self.assertRegex(body, r"setTimeout\(r, \d+\)")

    def test_concurrent_runs_are_refused(self):
        body = _src()
        body = body[body.index("async function carryPrivateToRelays"):body.index("async function _carryIfRelaysChanged")]
        self.assertIn("if(_carrying) return", body)
        self.assertIn("finally { _carrying = false; }", body)

    def test_there_is_a_manual_button(self):
        s = _src()
        self.assertIn('id="set-relay-carry"', s)
        self.assertIn("$('#set-relay-carry')", s)


if __name__ == "__main__":
    unittest.main()
