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
import json
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
CONCORD = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")


def _fn(header):
    at = CONCORD.index(header)
    end = CONCORD.index("\n  }", at)
    return re.sub(r"/\*.*?\*/", "", CONCORD[at:end], flags=re.S)


def _filter_admits(rooms):
    """RUN the shipped `wanted` filter of persistArmadaMemberships under node, per room."""
    body = _fn("  async function persistArmadaMemberships(p,rooms){")
    m = re.search(r"\.filter\((room=>.*?)\);\n", body, re.S)
    assert m, "persistArmadaMemberships no longer filters the rooms it is given"
    js = ("function roomIdentity(room){ return String(room&&(room.communityId||room.naddr||room.url)||''); }\n"
          "const f=(%s);\nprocess.stdout.write(JSON.stringify(%s.map(r=>!!f(r))));" % (m.group(1), json.dumps(rooms)))
    return json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)


def _key_expr(src):
    """The expression a membership vault key is derived from, spelled one way."""
    return src.replace("room.cord?.bundle?.", "bundle.").replace("room.cord&&room.cord.bundle.", "bundle.")


class TestTheVaultKeyIsTheRoomsIdentity(unittest.TestCase):
    """Since 2bf5cd4e7 (canonical CORD-02 33302 fragments) the WIRE key is the community's 32-byte
    commitment -- `cordListB64(bundle.community_id||room.communityId)` -- and an old invite-address
    key is migrated to it on read (`cordMergeLists`). The rules below are the same ones this file
    was written for; only the spelling of the key moved."""

    def test_persist_no_longer_demands_a_community_id(self):
        body = _fn("  async function persistArmadaMemberships(p,rooms){")
        self.assertNotIn("!room.communityId", body,
                         "a room joined by invite link has none, and refusing it is the bug")
        # A room carrying its bundle (every invite join does) is admitted without a communityId.
        self.assertEqual(_filter_admits([{"url": "https://x/invite/a#k",
                                          "cord": {"bundle": {"community_id": "ab" * 32}}}]), [True])
        self.assertIn("community_id:cid", body)
        self.assertRegex(body, r"cid=cordListB64\(bundle\.community_id\|\|room\.communityId\)",
                         "the entry must be keyed on the community commitment the bundle carries")

    def test_it_still_needs_an_invite_url(self):
        """The url carries the `#fragment`, which is the key. Without it another device can list
        the room and never open it, which is worse than not listing it. 1adc591c5 (direct invites)
        deliberately also admits a room whose BUNDLE is held -- the bundle itself carries the keys."""
        self.assertEqual(_filter_admits([
            {"communityId": "abc"},                                   # no url, no bundle: refused
            {"communityId": "abc", "url": "https://x/invite/a#k"},    # invite url: kept
            {"communityId": "abc", "cord": {"bundle": {"community_id": "ab" * 32}}},  # keys held
            None,
        ]), [False, True, True, False],
            "a room with no invite url and no key material must be filtered out, not written keyless")
        body = _fn("  async function persistArmadaMemberships(p,rooms){")
        self.assertIn("invite_ref:room.url", body)

    def test_leaving_uses_the_same_identity(self):
        body = _fn("  async function leaveArmadaMembership(p,room){")
        self.assertIn("const cid=roomIdentity(room);", body)
        self.assertNotIn("if(!room||!room.communityId)return true;", body,
                         "leaving such a room used to succeed silently, writing no tombstone")
        m = re.search(r"const (\w+)=cordListB64\(([^;]*?)\);", body)
        self.assertTrue(m, "leave no longer derives a wire key")
        var, leave_key = m.group(1), _key_expr(m.group(2))
        self.assertIn("tombs.set(%s," % var, body)
        self.assertIn("entries.delete(%s)" % var, body)
        persist = _fn("  async function persistArmadaMemberships(p,rooms){")
        join_key = _key_expr(re.search(r"cid=cordListB64\(([^;]*?)\);", persist).group(1))
        self.assertTrue(leave_key.startswith(join_key),
                        "leave keys its tombstone on %r but join keys the entry on %r -- a left room "
                        "comes straight back from the vault" % (leave_key, join_key))


class TestOneWritePerPass(unittest.TestCase):
    """13302/33302 ARE REPLACEABLE, SO EVERY WRITE REWRITES THE WHOLE DOCUMENT.

    Called once per room, persist reads the prior document, adds one entry and publishes the lot.
    In a loop it races itself: the second call's READ can be answered before the first call's
    publish is queryable, so the document it writes is the old one plus its own entry -- dropping
    the entry before it. Awaiting each call orders the PUBLISHES; it is the reads that overlap.

    Measured on a real account: the backfill ran over three rooms and the vault afterwards held the
    last two, written 18:20:09 and 18:20:17, with the first missing entirely.

    Since 2bf5cd4e7 the read-modify-write lives in `cordWriteMembership`, which is serialised per
    account; persist hands it ONE change that folds in every room.
    """

    def test_the_list_is_the_unit_of_work(self):
        body = _fn("  async function persistArmadaMemberships(p,rooms){")
        self.assertEqual(body.count("cordWriteMembership("), 1,
                         "more than one write per call is the race again")
        self.assertNotRegex(body, r"p\.publish\(|membershipEvents\(",
                            "persist must not read or publish around the serialised writer")
        cb = body[body.index("cordWriteMembership("):body.index("return {list,changed};")]
        self.assertIn("for(const room of wanted)", cb,
                      "every room must be folded into the one document before it is published")
        writer = CONCORD[CONCORD.index("  async function cordWriteMembership(p,change){"):]
        writer = writer[:writer.index("\n  async function ")]
        self.assertIn("const prior=membershipWrites.get(owner)", writer)
        self.assertIn("prior.catch(()=>{}).then(", writer,
                      "two writes for one account must queue, or their reads overlap")
        self.assertIn("membershipWrites.set(owner,job)", writer)

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
