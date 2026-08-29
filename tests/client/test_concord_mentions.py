"""Being mentioned has to reach you, in any channel and in any kind of room.

Two things stopped it, and neither showed up as an error anywhere:

  * notifyMentions was only ever called with `state.channel` — from the live merge and from
    render — so the only channel that could raise a notification was the one already on screen,
    which is the one you are reading. Hydration walks every channel in the room and said nothing.
  * its cursor was keyed on `room.naddr`, which a NIP-29 room and a room joined by community id do
    not have. Those rooms got no mention notifications at all: the guard returned before looking.

The runtime beside this drives the real notifyMentions, and each rule was verified to fail with the
fix removed.
"""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME = os.path.join(ROOT, "tests", "client", "concord_mentions_runtime.mjs")
CONCORD = os.path.join(ROOT, "static", "js", "client", "concord.js")


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class MentionsReachYou(unittest.TestCase):
    def test_mentions_notify_in_every_channel_and_every_kind_of_room(self):
        r = subprocess.run(["node", RUNTIME], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-4000:])

    def test_a_sent_reply_carries_the_tags_a_mention_is_read_from(self):
        """The other half of "tag a user": the composer must put the person on the wire. `P`/`p`
        for each mentioned pubkey is what notifyMentions reads on the receiving side, and what
        Armada reads too."""
        src = open(CONCORD, encoding="utf-8").read()
        send = src[src.index("send.onclick=async()=>{"):]
        send = send[:send.index("\n")]
        self.assertIn("mentionTags.push(['P',pk],['p',pk])", send,
                      "a typed mention no longer tags the person it names")
        self.assertIn("typedMentionRecipients(text", send,
                      "the composer stopped resolving typed @handles to pubkeys")
