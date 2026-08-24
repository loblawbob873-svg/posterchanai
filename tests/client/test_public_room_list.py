"""Chat shows public rooms you have not been in yet.

Run: venv-unified/bin/python -m pytest tests/client/test_public_room_list.py

`renderChatrooms` queries 200 kind-40 channel definitions and then showed only the ones with kind-42
activity ON THIS INSTANCE, plus the ones you joined or created. The reasoning was sound as far as it
went — "showing 50 empty foreign channels is noise" — but it left the screen with no way to FIND a
room: a channel appeared only once it had messages here, and somebody has to be the first to speak
in it. Chicken and egg, and the egg was unreachable. Reported as "no public room list?".

They are not mixed into the first grid, which stays what it was. They go underneath, named and
counted, so the first thing you see is still what is active/joined/yours.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
CSS = os.path.join(ROOT, "static", "css", "client.css")


def _fn(src, head):
    i = src.index(head)
    j = src.index("{", i)
    depth, k = 0, j
    while k < len(src):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
        k += 1
    raise AssertionError(f"{head} never closes")


def _decomment(js):
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", js)


class ThePublicRoomList(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(APP, encoding="utf-8") as fh:
            cls.body = _decomment(_fn(fh.read(), "async function renderChatrooms()"))
        with open(CSS, encoding="utf-8") as fh:
            cls.css = fh.read()

    def test_channels_with_no_local_activity_are_still_offered(self):
        self.assertIn("Public rooms", self.body,
                      "a channel is invisible until somebody speaks in it here, and nobody can get "
                      "to it to speak")

    def test_it_costs_no_extra_query(self):
        """The definitions are already fetched — they were being DISCARDED. The three queries here
        (kinds 40, 42 and 41) predate the public list and go out together in one Promise.all, so
        this list costs nothing: a fourth, or any of them moved out of that batch, is the
        regression."""
        self.assertEqual(self.body.count("Relay.query("), 3,
                         "the number of relay queries in this view changed")
        # To the end of that STATEMENT's line — `index("])")` lands inside the first
        # `Relay.query([{…}])` and cuts the batch in half.
        i = self.body.index("Promise.all([")
        batch = self.body[i:self.body.index("\n", i)]
        self.assertEqual(batch.count("Relay.query("), 3,
                         "a query left the batch, so the view now waits on two round trips")

    def test_the_active_grid_is_still_first_and_still_filtered(self):
        """Discovery must not turn the first screen into a directory."""
        self.assertLess(self.body.index("shown.map(channelCard)"),
                        self.body.index("restShown.map(channelCard)"),
                        "the public rooms render above what is active, joined or yours")
        for gate in ("active.has(c.id)", "PUBCHATS.has(c.id)", "c.pubkey===ME.pubkey"):
            self.assertIn(gate, self.body, f"the first grid lost its {gate} rule")

    def test_the_two_lists_never_show_the_same_room_twice(self):
        self.assertIn("seen=new Set(shown.map", self.body)
        self.assertIn("!seen.has(c.id)", self.body)

    def test_deleted_channels_are_excluded_from_BOTH(self):
        """A room somebody deleted must not reappear in the new list."""
        self.assertIn("_isChanDeleted", self.body)
        # `live` is the filtered set both lists are built from.
        self.assertIn("const live=chans.filter", self.body)
        self.assertIn("live.filter(c=> !seen.has(c.id))", self.body)

    def test_the_list_is_bounded(self):
        """200 definitions is a wall of cards on a phone."""
        self.assertIn("REST_MAX", self.body)
        self.assertIn("rest.slice(0, REST_MAX)", self.body)
        self.assertIn("more.", self.body, "a truncated list must say it was truncated")

    def test_the_section_is_styled(self):
        for cls in (".chat-sec{", ".chat-sec-note{"):
            self.assertIn(cls, self.css, f"{cls} has no rule, so the heading is unstyled text")


if __name__ == "__main__":
    unittest.main()
