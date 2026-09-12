"""Mail must offer a way to reach EVERY folder, not just the three pinned chips.

`.mail-folders` is one row by design — two rows cost 130px of a 553px phone, permanently — so it
shows INBOX/Sent/Drafts, the folder you are in, and a "More" popover for the rest. Two consequences,
and together they are the report ("Email needs a way to browse the damn email folders!", and before
that "no folder browsing — need nice UI to add this without losing screen space"):

  * the More button is rendered ONLY when `_folderChoices()` returned something beyond the pinned
    three, so a mailbox whose folder list has not loaded — or All-inboxes before `_allFolders`
    lands — offers no way out of Inbox at all; and
  * a popover anchored to a chip inside a sideways-scrolling strip is not a folder browser.

The browser is a sheet, so it costs nothing when closed. These assertions run against the SHIPPED
source with comments stripped, because the fix's own comment names every symbol they look for.
"""
import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "app.js"


def _code_only(src):
    out, i, n = [], 0, len(src)
    while i < n:
        two = src[i:i + 2]
        if two == "/*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(" " * (j - i)); i = j
        elif two == "//":
            j = src.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i)); i = j
        else:
            out.append(src[i]); i += 1
    return "".join(out)


class EmailFolderBrowsing(unittest.TestCase):
    def setUp(self):
        self.src = _code_only(APP.read_text())
        m = re.search(r"    browseFolders\(\)\{.*?\n    \},", self.src, re.S)
        self.assertIsNotNone(m, "browseFolders() is gone — there is no folder browser")
        self.browse = m.group(0)

    def test_there_is_a_control_that_opens_it(self):
        """A browser nothing can open is not a browser."""
        self.assertIn("mail-folders-open", self.src,
                      "no Browse folders control in the mail toolbar")
        self.assertRegex(self.src, r"#mail-folders-open[^\n]*onclick[^\n]*browseFolders\(\)",
                         "the Browse folders button is not wired to browseFolders()")

    def test_it_lists_every_folder_not_just_the_pinned_ones(self):
        """The strip's pinned three are exactly what the user could already reach."""
        self.assertIn("_folderChoices()", self.browse,
                      "the browser does not read the full folder list")
        self.assertNotRegex(self.browse, r"\['INBOX'\s*,\s*'Sent'\s*,\s*'Drafts'\]",
                            "the browser pins the same three folders the strip already showed")

    def test_a_folder_click_actually_navigates(self):
        self.assertIn("selectFolder(", self.browse,
                      "clicking a folder in the browser does not open it")
        self.assertIn("closeModal()", self.browse,
                      "the sheet stays open over the folder it just opened")

    def test_an_unread_folder_list_says_so_and_can_be_fetched(self):
        """The case that made this unreachable: no folders loaded, so no More button."""
        self.assertIn("hasn't been read yet", self.browse,
                      "an unloaded folder list is presented as an empty mailbox")
        self.assertIn("loadFolders()", self.browse,
                      "there is no way to fetch the folder list from the browser")

    def test_it_costs_nothing_when_closed(self):
        """The stated constraint: no permanent chrome. A sheet, not a second strip."""
        self.assertIn("modal(", self.browse,
                      "the browser is not an on-demand sheet")
        # it must not add a second always-present row to the mail layout
        self.assertNotIn("mail-folders-2", self.src)

    def test_the_filter_exists_because_real_mailboxes_are_large(self):
        self.assertIn("mail-fbrowse-q", self.browse, "no filter box in the folder browser")


if __name__ == "__main__":
    unittest.main()
