"""A poll from Armada was a question with no answers.

Armada's reader accepts kind 1068 beside 9 and 1111, so a poll posted there arrives in Concord as an
ordinary message — the question as its content, the options in `option` tags. concord.js had no
reference to 1068 at all, so it drew the question as bare text and the options nowhere.

The answers are kind-1018 votes sealed inside the same channel stream: foldTimeline folds them into
pollVotes, and inspectChat dropped that on the floor. Nothing outside the stream can count them —
a public NIP-88 tally query finds none of them — so the reader had to hand them over.

Deliberately NOT the timeline's pollCard: that is a note with an avatar, a header and an action bar,
none of which belongs in a chat bubble.
"""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUNTIME = os.path.join(ROOT, "tests", "client", "concord_polls_runtime.mjs")
CONCORD = os.path.join(ROOT, "static", "js", "client", "concord.js")
CSS = os.path.join(ROOT, "static", "css", "client.css")


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class PollsRender(unittest.TestCase):
    def test_a_poll_draws_its_options_and_counts_each_voter_once(self):
        r = subprocess.run(["node", RUNTIME], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-4000:])

    def test_the_bubble_actually_draws_the_poll(self):
        """The runtime calls pollHtml directly, so this is what holds the WIRING: unhooked, the
        renderer is dead code and every assertion beside it still passes."""
        src = open(CONCORD, encoding="utf-8").read()
        body = src[src.index("function messageContentHtml("):]
        body = body[:body.index("\n  function ", 10)]
        self.assertIn("pollHtml(p,m)", body, "the message bubble no longer draws polls")
        self.assertIn("body+poll+", body, "the poll is built but never returned")

    def test_voting_publishes_into_the_channel_and_not_to_the_open_relay(self):
        """A Concord vote is sealed in the channel stream like every other message. Published
        publicly it would be both unreadable to the room and visible to everyone outside it."""
        src = open(CONCORD, encoding="utf-8").read()
        vote = src[src.index("$$('[data-cc-poll]')"):]
        vote = vote[:vote.index("$$('[data-cc-thread]')")]
        self.assertIn("publishCordMessage(p,room,state.channel,'',tags,1018)", vote)
        self.assertIn("['e',id]", vote, "the vote does not name the poll it answers")
        self.assertIn("['response',o]", vote, "the vote does not use Armada's response tag")

    def test_the_poll_has_styling_of_its_own(self):
        """An unstyled widget inside a chat bubble is worse than the bare text it replaced."""
        css = open(CSS, encoding="utf-8").read()
        for cls in (".cc-poll{", ".cc-poll-opt{", ".cc-poll-bar{", ".cc-poll-foot{"):
            self.assertIn(cls, css, "%s has no styling" % cls)
