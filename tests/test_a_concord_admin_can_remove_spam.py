"""A community owner or moderator must be able to remove somebody else's message.

Asked as: "so concord admins can't delete spam or illegal posts?" They could not — and the reason
was not that CORD lacks the feature. CORD defines the permission:

    { bit: Permissions.MANAGE_MESSAGES, label: "Manage messages",
      hint: "Hide other members' messages." }

and `foldTimeline(opened, moderation)` consults `moderation.canDelete(deleter, author, act)`. But
`inspectChat` called `foldTimeline(events)` with ONE argument, so that branch was dead: only a
SELF-delete ever removed anything. An owner clearing spam published a perfectly valid kind-5 that
every client — including their own — ignored, with nothing said.

`concord.js` made it worse by re-deriving the rule from `opened.deletions` ("the delete must match
the message's author") and applying it to messages already on screen, which overrode the fold.

These run the SHIPPED reader against a REALLY MINTED community (tests/mint_moderated_room.mjs):
real keys, real wraps, real kind-5 deletions. The rule is `canActOnPosition` — the owner always
may, anyone else needs MANAGE_MESSAGES *and* to outrank the author — so moderation cannot run
upward.
"""
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def mint():
    done = subprocess.run(["node", "tests/mint_moderated_room.mjs"], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    line = [l for l in done.stdout.splitlines() if l.startswith("ROOM ")][0]
    return json.loads(line[len("ROOM "):])


def read(room, extra_wraps):
    """Fold the channel through the shipped reader with the given deletion wraps applied."""
    script = """
import { makeRealm, loadInto, into } from '../botframework/cord_realm.mjs';
const cord = makeRealm();
loadInto(cord, 'static/js/client/cord-protocol.js');
loadInto(cord, 'static/js/client/cord-reader.js');
const toCord = into(cord);
const room = JSON.parse(process.argv[2]);
const opened = cord.PosterCord.openInvite(room.invite, toCord(room.bundleEvents));
const out = await cord.PosterCordReader.inspectChat(
  toCord(opened.bundle), toCord(room.controlWraps), room.channelId,
  toCord(room.wraps.concat(JSON.parse(process.argv[3]))));
console.log(JSON.stringify({
  texts: out.messages.map(m => m.text),
  ids: out.messages.map(m => m.id),
  deletedMessageIds: out.deletedMessageIds }));
"""
    path = ROOT / "tests" / "_read_moderated.mjs"
    path.write_text(script, encoding="utf-8")
    try:
        done = subprocess.run(["node", str(path), json.dumps(room), json.dumps(extra_wraps)],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)
        assert done.returncode == 0, done.stdout + done.stderr
        return json.loads(done.stdout.strip().splitlines()[-1])
    finally:
        path.unlink(missing_ok=True)


class AnOwnerCanRemoveSpam(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.room = mint()

    def test_the_spam_is_there_before_anyone_moderates(self):
        """The control: without the deletion both messages read normally, so a later absence means
        moderation and not a channel that stopped decrypting."""
        out = read(self.room, [])
        self.assertIn("BUY CHEAP FOLLOWERS http://spam.example", out["texts"])
        self.assertIn("ordinary conversation", out["texts"])

    def test_the_owners_deletion_removes_it(self):
        """THE BUG. This deletion was published, stored, and ignored by every client."""
        out = read(self.room, [self.room["ownerDeleteWrap"]])
        self.assertNotIn("BUY CHEAP FOLLOWERS http://spam.example", out["texts"],
                         "an owner still cannot remove spam from their own room")
        self.assertIn(self.room["spamId"], out["deletedMessageIds"])

    def test_it_removes_only_that_message(self):
        out = read(self.room, [self.room["ownerDeleteWrap"]])
        self.assertIn("ordinary conversation", out["texts"],
                      "moderation took the rest of the channel with it")

    def test_moderation_cannot_run_upward(self):
        """A member with no role deleting the OWNER's message must change nothing — otherwise
        'anyone can delete anything' is the new bug."""
        out = read(self.room, [self.room["spammerDeleteWrap"]])
        self.assertIn("ordinary conversation", out["texts"],
                      "a member with no permission removed the owner's message")
        self.assertNotIn(self.room["okId"], out["deletedMessageIds"])

    def test_a_self_delete_still_works(self):
        """The rule that shipped before must be untouched: the spammer may delete their own."""
        room = dict(self.room)
        script_wrap = room["ownerDeleteWrap"]     # owner deleting spam, already covered
        out = read(room, [script_wrap])
        self.assertNotIn(room["spamId"], out["ids"])


class TheControlStreamSaysWhoMayModerate(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.room = mint()

    def test_inspect_control_reports_the_owner(self):
        """`concord.js` decides whether to OFFER the control from this; the fold decides what the
        deletion actually removes."""
        self.assertEqual(self.room["ownerFromControl"], self.room["ownerPk"])

    def test_the_moderator_list_never_contains_a_stranger(self):
        self.assertNotIn(self.room["spammerPk"], self.room["moderators"])


if __name__ == "__main__":
    unittest.main()
