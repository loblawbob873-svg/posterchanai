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

from app.services.nostr_relay.server import _BACKUP_NS, _broadcastable, _private_mirrorable


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


class PrivateMirror(unittest.TestCase):
    """The second path: the encrypted libraries go to the operator's OWN relays, and only there.

    Everything public is already on twenty relays; the irreplaceable half was on one. This is the
    redundancy for that half — and the reason it is a separate list rather than the public upstreams
    is the metadata trail, not the contents.
    """

    def test_the_private_libraries_are_mirrorable(self):
        for d in ("pcai:note:abc", "pcai:notefolder:work", "pcai:pw:abc", "pcai:pwfolder:banking",
                  "pcai:pwkey", "pcai:budget", "pcai:files-index", "pcai:files-index-bak:1",
                  "pcai:drafts", "pcai:voices", "pcai:news-feeds", "pcai:news-read",
                  "pcai:client-prefs", "pcai:conv:x", "pcai:msg:x", "pcai:upload:c:1"):
            with self.subTest(d):
                self.assertTrue(_private_mirrorable(ev(30078, d)))

    def test_unpublished_drafts_are_mirrorable(self):
        """A NIP-23 article draft is withheld from the public network because it is not finished —
        which also left the purest case of 'exists once, irreplaceable' with no second copy."""
        for kind in (30024, 30403):
            with self.subTest(kind=kind):
                self.assertTrue(_private_mirrorable(ev(kind, "draft")))
                self.assertFalse(_broadcastable(ev(kind, "draft"), {"backup_datastore": True}))

    def test_the_mirror_skips_an_event_the_relay_already_had(self):
        """Two nodes pointed at each other — the recommended topology — would otherwise bounce every
        private event between them forever: add_event reports True for a DUPLICATE, so `stored`
        alone is not 'this is new'."""
        import inspect

        from app.services.nostr_relay.server import RelayServer
        src = inspect.getsource(RelayServer)
        self.assertIn("if self.private_cb and _was_new and _private_mirrorable(ev):", src)
        self.assertIn("_was_new = not await self.store.has_event(eid)", src)

    def test_config_docs_are_not_on_the_private_path(self):
        """They already have their own route (backup_datastore → the public upstreams)."""
        for d in ("pcai:setting:llm_model", "pcai:user:abc", "pcai:usercfg:abc", "pcai:bot:1"):
            with self.subTest(d):
                self.assertFalse(_private_mirrorable(ev(30078, d)))

    def test_nothing_else_is(self):
        self.assertFalse(_private_mirrorable(ev(1)))
        self.assertFalse(_private_mirrorable(ev(30078, "some-other-app")))
        self.assertFalse(_private_mirrorable(ev(30078)))          # no d tag at all
        self.assertFalse(_private_mirrorable(ev(30023, "pcai:note:abc")))   # right d, wrong kind

    def test_the_two_paths_never_overlap(self):
        """An event on both would be published twice, and one of those is the public network."""
        for d in ("pcai:note:abc", "pcai:pw:abc", "pcai:pwkey", "pcai:budget",
                  "pcai:setting:x", "pcai:user:x", "pcai:usercfg:x", "pcai:bot:x"):
            with self.subTest(d):
                e = ev(30078, d)
                for cfg in ({}, {"backup_datastore": True}, {"backup_datastore": False}):
                    self.assertFalse(_broadcastable(e, cfg) and _private_mirrorable(e),
                                     "%s would be sent down both paths" % d)

    def test_a_lookalike_namespace_is_not_swept_in(self):
        """startswith on a bare prefix is how an unrelated doc gets mirrored by accident."""
        self.assertFalse(_private_mirrorable(ev(30078, "pcai:notify:x")))
        self.assertFalse(_private_mirrorable(ev(30078, "pcai:power:x")))


class MirrorWiring(unittest.TestCase):
    """Off by default, and off means the decision cannot be reached at all."""

    def test_the_setting_exists_and_defaults_to_blank(self):
        from app.schemas import SettingsResponse
        f = SettingsResponse.model_fields["nostr_relay_private_relays"]
        self.assertIsNone(f.default, "mirroring must be something an operator turned on")

    def test_the_server_only_mirrors_when_a_callback_was_given(self):
        import inspect

        from app.services.nostr_relay.server import RelayServer
        self.assertIn("private_cb", inspect.signature(RelayServer.__init__).parameters)
        src = inspect.getsource(RelayServer)
        self.assertIn("if self.private_cb and _private_mirrorable(ev):", src)

    def test_no_relays_means_no_second_outbox(self):
        import inspect

        from app.services.nostr_relay import thread
        src = inspect.getsource(thread)
        self.assertIn('if cfg["private_relays"]:', src)
        self.assertIn("private_cb=(private.enqueue if private else None)", src)


    def test_the_namespace_list_is_explicit(self):
        """Bare prefixes would adopt a future namespace into the copied-off-the-box set."""
        from app.services.nostr_relay.server import _PRIVATE_DOCS, _PRIVATE_NS
        self.assertEqual(_PRIVATE_NS,
                         ("pcai:note:", "pcai:notefolder:", "pcai:pw:", "pcai:pwfolder:",
                          "pcai:cal:", "pcai:calmeta:",
                          "pcai:files-index-bak:", "pcai:conv:", "pcai:msg:", "pcai:upload:"))
        self.assertEqual(_PRIVATE_DOCS,
                         ("pcai:pwkey", "pcai:budget", "pcai:files-index", "pcai:drafts",
                          "pcai:voices", "pcai:news-feeds", "pcai:news-read", "pcai:client-prefs"))
        for d in ("pcai:notes-export:1", "pcai:pwpolicy", "pcai:budgeting"):
            self.assertFalse(_private_mirrorable(ev(30078, d)), d)

    def test_the_calendar_and_addressbook_are_mirrored_like_every_other_library(self):
        """They were the one private library with NO second copy anywhere.

        A calendar item and a contact card are the same shape and the same risk as a note: one
        encrypted event, one Postgres, no other copy. They were simply missing from _PRIVATE_NS, so
        "sync my data" handed back the notes and the vault and left the calendar and the phone's
        addressbook empty — a restore that looks like it worked.

        `pcai:calmeta:` is asserted alongside the items on purpose: it IS the collection (name,
        colour, and the `kind` that tells a VADDRESSBOOK from a calendar). Mirroring the events
        without it restores items into a calendar that does not exist, which no client shows.
        """
        for d in ("pcai:cal:default:8f21-4c.ics", "pcai:cal:contacts:ab12.vcf",
                  "pcai:calmeta:default", "pcai:calmeta:contacts"):
            self.assertTrue(_private_mirrorable(ev(30078, d)), d)

    def test_the_libraries_are_restored_from_the_private_relays_not_the_public_ones(self):
        """A backfill that asks `upstream` for kind 30078 finds nothing, by design.

        _broadcastable withholds every `pcai:` document from the public relays, so the ONLY copies
        are on the operator's private mirrors. Asking the public set restored a user's posts and none
        of their notes, passwords or calendar — and adding 30078 to the PUBLIC pass instead would be
        worse than useless: with `backup_datastore` on, that kind carries a node's own
        pcai:setting:/user:/bot: docs upstream, so a per-user button could pull another node's
        settings into this store.
        """
        import inspect

        from app.services.nostr_relay import ingest
        self.assertEqual(ingest._PRIVATE_LIB_KINDS, [30024, 30078, 30403])
        src = inspect.getsource(ingest.backfill_author)
        self.assertIn("private_relays", src)
        # The private kinds go to the private relay set, and the public pass must not name them.
        self.assertIn("_backfill_filter(store, server, private_relays,", src)
        public = src.split("if private_relays:")[0]
        self.assertNotIn("_PRIVATE_LIB_KINDS", public)
        for k in (30078, 30024, 30403):
            self.assertNotIn(str(k), public.split("kinds = kinds or")[1].split("\n")[0])

    def test_the_relay_thread_hands_the_private_relays_to_the_backfill(self):
        """The wiring, not just the capability: an unpassed argument defaults to None and the
        restore silently does nothing while every log line still says the sync ran."""
        import inspect

        from app.services.nostr_relay import thread
        src = inspect.getsource(thread)
        self.assertIn('private_relays=cfg.get("private_relays")', src)


if __name__ == "__main__":
    unittest.main()


class ReplaceableTieBreak(unittest.TestCase):
    """NIP-01: on EQUAL created_at the LOWER event id wins.

    The store handed a tie to the newcomer and DELETED the incumbent. Non-conformant on its own, and
    it makes two mutually-mirroring relays disagree forever: save the same note from two devices
    inside one second, and each node flips to whatever the other last sent it. The losing version is
    deleted, so it looks new again on the way back, which re-arms the mirror's has_event guard on
    every flip. The rule has to be total and identical on both ends.
    """

    def test_the_rule_is_lowest_id_on_a_tie(self):
        import inspect

        from app.services.nostr_relay.store import RelayStore
        src = inspect.getsource(RelayStore._insert_one)
        self.assertIn('older = row["created_at"] < created', src,
                      "a strictly-older incumbent is the only one a later event replaces")
        self.assertIn('tie_lost = row["created_at"] == created and eid < row["id"]', src)
        self.assertNotIn('row["created_at"] <= created', src,
                         "<= hands every tie to whoever wrote last — the flip-flop")

    def test_the_mirror_says_something_when_it_cannot_check(self):
        """Silence there turns the backup off while it still looks on."""
        import inspect

        from app.services.nostr_relay.server import RelayServer
        self.assertIn("private mirror skipped (has_event failed)", inspect.getsource(RelayServer))

    def test_the_mirror_has_its_own_queue_budget_and_its_own_log_line(self):
        """The public outbox is paced to be polite to strangers; this one holds the only copy."""
        import inspect

        from app.services.nostr_relay import thread
        src = inspect.getsource(thread)
        self.assertIn('label="private-mirror"', src)
        self.assertIn("min_interval=0.05", src)
        from app.services.nostr_relay.outbox import Outbox
        self.assertEqual(inspect.signature(Outbox.__init__).parameters["label"].default, "outbox")
        self.assertIn("%s queue full", inspect.getsource(Outbox.enqueue))
