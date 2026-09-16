"""A TOMBSTONE ALONE MUST NOT HIDE A COMMUNITY FOR EVER.

`dead` counts a tombstone with NO membership entry as winning: `added_at` defaults to 0 and every
timestamp beats it. That is right for hiding the room on one pass and very wrong as a PERMANENT
local record -- `wasLocallyLeft` then refuses that community on every future pass, including after a
fresh join, and nothing on screen says why.

Reported the same day the seeding shipped: "my concord community for posterchan is no longer
appearing in Concord". The seeding itself exists for a real bug -- a leave that did not survive to
another device, because the vault tombstone is keyed on `community_id` and an invite announcement
carries none -- and that case has BOTH a tombstone and an entry, since leaving something you joined
leaves both behind. So the ledger is taught only by a tombstone that beat a real entry.

And it heals: an entry that beats its tombstone means this account is IN that community, so a local
record saying otherwise is stale and is cleared. Without that, a device already poisoned stays
poisoned no matter what the fix does.
"""
from pathlib import Path
import json
import re
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
CONCORD = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")


def sync_body():
    body = CONCORD[CONCORD.index("async function syncArmadaMemberships("):]
    return body[: body.index("\n  async function ")]


class TestOnlyARealLeaveTeachesTheLedger(unittest.TestCase):
    def test_a_tombstone_with_no_entry_is_skipped(self):
        """RUN the loop's own skip condition. A tombstone with no entry and nothing identifying the
        invite it retired is the bare tombstone this file is about, and must teach nothing.

        2bf5cd4e7 (canonical CORD-02 fragments) deliberately narrowed that: a CORD-02 leave REMOVES
        the entry from its fragment and writes the tombstone with the `invite_ref`/`naddr` it
        retired, so an entry-less tombstone carrying one of those is a genuine leave, not a bare one.
        """
        body = sync_body()
        loop = body[body.index("for(const id of dead){"):]
        loop = loop[: loop.index("\n      }")]
        loop = re.sub(r"/\*.*?\*/", "", loop, flags=re.S)
        m = re.search(r"\n\s*if\((.*?)\)\s*continue;", loop)
        self.assertTrue(m, "nothing in the dead loop can skip a tombstone any more")
        cases = [
            ({}, {}),                                           # bare tombstone: skipped
            ({"id": {"x": 1}}, {}),                             # a real entry beside it
            ({}, {"id": {"invite_ref": "https://x/i#k"}}),       # CORD-02 leave, invite named
            ({}, {"id": {"naddr": "naddr1xyz"}}),                # CORD-02 leave, naddr named
            ({}, {"id": {"invite_ref": "", "naddr": ""}}),       # names nothing: still bare
        ]
        js = ("const cases=%s;process.stdout.write(JSON.stringify(cases.map(([e,r])=>{"
              "const id='id',entries=new Map(Object.entries(e)),tombRefs=new Map(Object.entries(r));"
              "return !!(%s);})));" % (json.dumps(cases), m.group(1)))
        skipped = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True,
                                            check=True).stdout)
        self.assertEqual(skipped, [True, False, False, False, True],
                         "a bare tombstone can still write a permanent local 'left' record")

    def test_it_still_records_a_genuine_leave(self):
        """Both halves present is what a real leave looks like; that is the bug this fixes."""
        loop = sync_body()
        loop = loop[loop.index("for(const id of dead){"):]
        loop = loop[: loop.index("\n      }")]
        self.assertIn("noteLeftFromVault(", loop)

    def test_the_pass_still_hides_the_room(self):
        """Skipping the LEDGER must not un-hide the room for this pass: `dead` still filters."""
        body = sync_body()
        self.assertIn("!dead.has(room.communityId)", body)


class TestItHealsADevicveAlreadyPoisoned(unittest.TestCase):
    def test_a_live_entry_clears_a_stale_left_record(self):
        body = sync_body()
        self.assertIn("forgetLeftCommunity(", body,
                      "a device that already recorded the wrong thing stays wrong for ever")

    def test_the_heal_skips_communities_that_really_are_dead(self):
        body = sync_body()
        heal = body[body.index("for(const [id, entry] of entries){"):]
        heal = heal[: heal.index("\n      }")]
        self.assertIn("dead.has(id)", heal,
                      "the heal would resurrect a community the account genuinely left")

    def test_the_heal_refuses_to_act_without_a_tombstone_to_compare_against(self):
        """A relay that has not yet received the tombstone answers with the old JOIN and nothing
        else. Healing on that undoes a leave the person really made -- the empty-read wipe this
        codebase has paid for more than once. `concord_runtime.mjs` drives that stale relay."""
        body = sync_body()
        heal = body[body.index("for(const [id, entry] of entries){"):]
        heal = heal[: heal.index("\n      }")]
        self.assertIn("!tombs.has(id)", heal,
                      "an absent tombstone is treated as 'there is none' rather than 'I cannot "
                      "see one'")

    def test_it_only_writes_when_there_is_something_to_clear(self):
        """A blind write on every pass is a save on every pass."""
        heal = sync_body()
        heal = heal[heal.index("for(const [id, entry] of entries){"):]
        heal = heal[: heal.index("\n      }")]
        self.assertIn("if(wasLocallyLeft(", heal)


if __name__ == "__main__":
    unittest.main()
