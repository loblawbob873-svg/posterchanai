"""Every action on a chat result must be on BOTH rows.

A generated file arrives either as an `/api/files/` artifact or as a base64 payload, and each renders
its own button row. They are meant to be the same row; they are two template literals. That is how
"no Meme Builder after a geni" happened — the artifact row had the button for months and the payload
row never did — and adding Save to Notes put the same trap one edit away again.

So this compares the two rows to each other rather than to a list someone has to remember to update.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")


def _src():
    with open(APP, encoding="utf-8") as f:
        return f.read()


class MediaRows(unittest.TestCase):
    def _rows(self):
        rows = re.findall(r'return `<div class="fx-reply-row">\$\{(.+?)\}</div>`', _src())
        self.assertEqual(len(rows), 2, "expected the artifact row and the payload row")
        return [re.findall(r"[A-Za-z_][A-Za-z0-9_]*", r) for r in rows]

    def test_both_rows_offer_the_same_actions(self):
        a, b = self._rows()
        self.assertEqual(a, b,
                         "the two chat action rows have drifted — a result rendered one way offers "
                         "an action the other does not")

    def test_save_to_notes_is_on_both(self):
        for cls in ("ai-notefile", "ai-note-fx"):
            self.assertIn(cls, _src(), "%s is missing — one of the two rows cannot save to Notes" % cls)

    def test_each_button_is_wired_to_a_handler(self):
        s = _src()
        for cls, fn in (("ai-notefile", "notesFromFileUrl"),
                        ("ai-note-fx", "notesFromEffectMedia")):
            self.assertRegex(s, r"closest\('\.%s'\)" % re.escape(cls),
                             "%s renders but nothing listens for it" % cls)
            self.assertIn("function %s(" % fn, s)

    def test_the_rows_do_not_carry_their_own_layout(self):
        """Two inline styles for one row is how they diverge in the first place."""
        self.assertNotRegex(_src(), r'fx-reply-row" style=',
                            "the row's layout belongs in client.css, once")

    def test_notes_is_saved_as_an_attachment_not_a_link(self):
        """An /api/files/ URL needs this session's cookie and does not outlive it.

        A note that linked one is a broken image tomorrow, and on every other device today.
        """
        s = _src()
        body = s[s.index("async function saveFileToNotes("):s.index("async function notesFromFileUrl(")]
        self.assertIn("files:[file]", body)
        body = s[s.index("async function notesFromFileUrl("):s.index("async function notesFromEffectMedia(")]
        self.assertIn("r.blob()", body, "the artifact must be fetched, not linked")


if __name__ == "__main__":
    unittest.main()
