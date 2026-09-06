"""A COMMUNITY YOU ARE IN BELONGS TO THE ACCOUNT, NOT TO THE DEVICE YOU JOINED IT ON.

Reported as "my room for posterchan is still not appearing", then narrowed by the person themselves
to "I see posterchan on laptop in concord but that is the only place", and confirmed on the web UI:
"i see 2 communities I joined on webui now, missing the posterchan one".

Measured on that account's laptop -- three joined rooms, and the one missing everywhere else was the
only one shaped like this:

    PosterChan   communityId (none)   cord.armadaList false   url https://poster.place/invite/…#…
    Gamers       communityId e3eb361d…  cord.armadaList true
    Lounge Chat  communityId 8dbb453b…  cord.armadaList true

and the decoded membership vault held entries and tombstones for the other two and NOTHING for
PosterChan. `persistArmadaMembership` refused, silently, any room without a `community_id` -- so a
room joined through a plain invite link was known to one browser profile for ever.

Two halves, both asserted here:
  * IDENTITY. The vault key is `roomIdentity(room)` -- community id, else naddr, else url -- which is
    what every other identity comparison in this file already uses. Leaving uses the same key, or a
    room the vault knows by its naddr could be left on one device and come straight back from the
    vault on the next.
  * BACKFILL. Persist runs on create/join/discover only, so a room joined before any of this existed
    is never written. The membership pass now publishes local rooms the vault has never heard of --
    guarded on having actually DECODED a document, because the empty entries/tombstones pair that a
    dead relay produces is indistinguishable from "you are in nothing", and republishing on that
    would put back every community the person ever left.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CONCORD = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")


def _fn(header):
    at = CONCORD.index(header)
    end = CONCORD.index("\n  }", at)
    return re.sub(r"/\*.*?\*/", "", CONCORD[at:end], flags=re.S)


class TestTheVaultKeyIsTheRoomsIdentity(unittest.TestCase):
    def test_persist_no_longer_demands_a_community_id(self):
        body = _fn("  async function persistArmadaMembership(p,room){")
        self.assertIn("const cid=roomIdentity(room);", body)
        self.assertNotIn("!room.communityId", body,
                         "a room joined by invite link has none, and refusing it is the bug")
        self.assertIn("community_id:cid", body)

    def test_it_still_needs_an_invite_url(self):
        """The url carries the `#fragment`, which is the key. Without it another device can list
        the room and never open it, which is worse than not listing it."""
        body = _fn("  async function persistArmadaMemberships(p,rooms){")
        self.assertIn("roomIdentity(room)&&room.url", body,
                      "a room with no invite url must be filtered out, not written keyless")
        self.assertIn("invite_ref:room.url", body)

    def test_leaving_uses_the_same_identity(self):
        body = _fn("  async function leaveArmadaMembership(p,room){")
        self.assertIn("const cid=roomIdentity(room);", body)
        self.assertIn("tombs.set(cid,", body)
        self.assertIn("entries.delete(cid)", body)
        self.assertNotIn("if(!room||!room.communityId)return true;", body,
                         "leaving such a room used to succeed silently, writing no tombstone")

    def test_roomIdentity_is_what_it_has_always_been(self):
        """The whole fix rests on this precedence; if it changes, the vault keys change with it."""
        self.assertIn("function roomIdentity(room){ return String(room&&"
                      "(room.communityId||room.naddr||room.url)||''); }", CONCORD)


class TestOneWritePerPass(unittest.TestCase):
    """13302 IS REPLACEABLE, SO EVERY WRITE REWRITES THE WHOLE DOCUMENT.

    Called once per room, persist reads the prior document, adds one entry and publishes the lot.
    In a loop it races itself: the second call's READ can be answered before the first call's
    publish is queryable, so the document it writes is the old one plus its own entry -- dropping
    the entry before it. Awaiting each call orders the PUBLISHES; it is the reads that overlap.

    Measured on a real account: the backfill ran over three rooms and the vault afterwards held the
    last two, written 18:20:09 and 18:20:17, with the first missing entirely.
    """

    def test_the_list_is_the_unit_of_work(self):
        body = _fn("  async function persistArmadaMemberships(p,rooms){")
        self.assertEqual(body.count("await p.publish(13302"), 1,
                         "more than one publish per call is the race again")
        self.assertEqual(body.count("membershipEvents(p,viewer.pubkey)"), 1,
                         "one read, or two calls can read the same stale document")
        self.assertIn("for(const room of wanted)", body,
                      "every room must be folded into the one document before it is published")

    def test_the_single_room_entry_point_delegates(self):
        self.assertIn("async function persistArmadaMembership(p,room){ return "
                      "persistArmadaMemberships(p,[room]); }", CONCORD,
                      "two implementations would drift, and the join path uses the single one")


class TestTheBackfill(unittest.TestCase):
    def setUp(self):
        at = CONCORD.index("if(recovered){")
        self.block = CONCORD[at: CONCORD.index("if(!live.length)", at)]

    def test_it_only_runs_on_a_vault_that_was_actually_read(self):
        self.assertTrue(self.block.startswith("if(recovered)"),
                        "an unread vault looks exactly like an empty one; republishing on it would "
                        "restore every community the person has left")

    def test_it_publishes_once_for_all_of_them(self):
        self.assertIn("persistArmadaMemberships(p,missing)", self.block,
                      "a room per call raced itself and lost the first room every time")
        self.assertNotIn("for(const room of rooms){\n        const rid", self.block)

    def test_it_skips_rooms_the_vault_already_knows(self):
        self.assertIn("!entries.has(rid)", self.block)

    def test_it_skips_a_tombstoned_room(self):
        self.assertIn("!tombs.has(rid)", self.block,
                      "re-adding a left room would be a resurrection loop between two devices")
        self.assertIn("wasLocallyLeft(viewer.pubkey,room)", self.block)

    def test_a_purely_local_room_stays_local(self):
        self.assertIn("room.url", self.block,
                      "a room with no invite url has nothing another device could open")

    def test_a_failed_publish_does_not_abort_the_pass(self):
        self.assertIn("catch(_)", self.block,
                      "an unreachable membership relay must not abort the whole pass")

    def test_it_runs_before_the_early_return(self):
        """`if(!live.length) return` fires for an account whose vault holds nothing yet -- which is
        exactly the account that most needs its local rooms written into it."""
        self.assertLess(CONCORD.index("if(recovered){"),
                        CONCORD.index("if(!live.length){if(changed)backgroundRender();return;}"))


if __name__ == "__main__":
    unittest.main()
