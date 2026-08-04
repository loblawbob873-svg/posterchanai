"""What this relay re-broadcasts to the 20 upstream public relays — and what it must never.

`_broadcastable` is the ONLY thing standing between a user's private library and the open network.
The relay's Outbox fans every direct client write out to `DEFAULT_RELAYS`; Notes (`pcai:note:`) and
Passwords (`pcai:pw:`) are kind-30078 direct client writes, so they meet that fan-out on every save.

The bodies are ciphertext, so this is not "your passwords leak". It is worse in one specific way:
it is UNRECALLABLE METADATA. A relay operator cannot read a vault entry, but the event carries the
author's pubkey, a stable `d` tag, and a timestamp — so a copy on twenty third-party relays is a
permanent, public, per-user record of how many passwords someone has and the exact moment each one
changed. Nothing can delete it afterwards.

This function had no test. The failure mode is not a crash or a wrong answer on screen; it is silent
and permanent, and the edit that causes it looks harmless — one more prefix in `_BACKUP_NS`, or an
early `return True` for a kind someone is adding.
"""
import unittest

from app.services.nostr_relay.server import _BACKUP_NS, _broadcastable


def ev(kind, d=None, tags=None):
    t = list(tags or [])
    if d is not None:
        t.append(["d", d])
    return {"kind": kind, "tags": t}


class NeverLeaves(unittest.TestCase):
    """The private libraries, under every configuration."""

    PRIVATE = [
        ("a note", "pcai:note:abc123"),
        ("a note folder", "pcai:notefolder:work"),
        ("a password", "pcai:pw:abc123"),
        ("a password folder", "pcai:pwfolder:banking"),
        ("the vault key", "pcai:pwkey"),
        ("the budget", "pcai:budget"),
        ("the files index", "pcai:files-index"),
    ]

    def test_not_broadcast_with_no_config(self):
        for what, d in self.PRIVATE:
            with self.subTest(what):
                self.assertFalse(_broadcastable(ev(30078, d), None))

    def test_not_broadcast_with_backup_off(self):
        for what, d in self.PRIVATE:
            with self.subTest(what):
                self.assertFalse(_broadcastable(ev(30078, d), {"backup_datastore": False}))

    def test_not_broadcast_with_backup_ON(self):
        """backup_datastore is ON BY DEFAULT, so this is the configuration that actually ships.

        Disaster recovery is a real feature and the operator's config genuinely belongs upstream —
        but the allowlist it opens must never widen to cover somebody's notebook or vault.
        """
        for what, d in self.PRIVATE:
            with self.subTest(what):
                self.assertFalse(_broadcastable(ev(30078, d), {"backup_datastore": True}))

    def test_the_backup_allowlist_covers_config_and_nothing_else(self):
        """Pinned as an exact tuple: the whole risk is this growing by one plausible-looking line."""
        self.assertEqual(_BACKUP_NS,
                         ("pcai:setting:", "pcai:user:", "pcai:usercfg:", "pcai:bot:"))
        for prefix in ("pcai:note:", "pcai:notefolder:", "pcai:pw:", "pcai:pwfolder:",
                       "pcai:pwkey", "pcai:budget", "pcai:files-index"):
            self.assertFalse(prefix.startswith(_BACKUP_NS),
                             "%s would be broadcast to every upstream relay" % prefix)

    def test_an_unknown_pcai_namespace_stays_local(self):
        """A namespace added later is private until someone deliberately says otherwise.

        Default-deny: the next feature to store something under `pcai:` should not have to know this
        file exists in order to stay off twenty public relays.
        """
        self.assertFalse(_broadcastable(ev(30078, "pcai:something-new:1"), {"backup_datastore": True}))


class DoesLeave(unittest.TestCase):
    """The other half: a guard that blocks everything is not a guard, it is a broken relay."""

    def test_public_kinds_are_broadcast(self):
        for kind in (0, 1, 3, 6, 7, 1059, 30023):
            with self.subTest(kind=kind):
                self.assertTrue(_broadcastable(ev(kind), {}))

    def test_config_docs_are_broadcast_only_with_backup_on(self):
        for d in ("pcai:setting:llm_model", "pcai:user:abc", "pcai:usercfg:abc", "pcai:bot:1"):
            with self.subTest(d):
                self.assertTrue(_broadcastable(ev(30078, d), {"backup_datastore": True}))
                self.assertFalse(_broadcastable(ev(30078, d), {"backup_datastore": False}))

    def test_a_plain_30078_from_another_app_is_not_ours_to_withhold(self):
        """Only the `pcai:` namespace is this node's internal state."""
        self.assertTrue(_broadcastable(ev(30078, "some-other-app"), {}))

    def test_drafts_and_nofederate_stay_local(self):
        self.assertFalse(_broadcastable(ev(30024, "draft"), {}))       # NIP-23 article draft
        self.assertFalse(_broadcastable(ev(30403, "draft"), {}))       # NIP-99 listing draft
        self.assertFalse(_broadcastable(ev(1, tags=[["nofederate"]]), {}))


if __name__ == "__main__":
    unittest.main()
