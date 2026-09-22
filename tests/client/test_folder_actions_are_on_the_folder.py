"""A destructive action must not sit in the folder NAVIGATION list.

"Delete “<name>”" was rendered as a `.folder-chip` in the Files sidebar, among the chips you click
to move between folders. The sidebar is 220px wide, so on any folder with a real name the label ran
out of the panel and the trash icon was clipped — reported as "the delete button doesn't even fit in
the space", "the icon is cut off" and "why would you put a delete button there". It also placed
Delete one mis-click away from the folder chip above it.

The action now lives on the folder itself (right-click / long-press), which is where every file
manager puts it. These assertions are written against the SHIPPED source and each fails if the chip
comes back or the replacement goes missing.
"""
import re
import unittest
from pathlib import Path
from tests.client_source import client_source

APP = Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "app.js"


class FolderActions(unittest.TestCase):
    def setUp(self):
        self.src = client_source()

    def test_delete_is_not_a_chip_in_the_navigation_list(self):
        """The regression."""
        side = re.search(r"function _fxSideHTML\(\)\{.*?\n  \}", self.src, re.S)
        self.assertIsNotNone(side, "_fxSideHTML not found")
        body = side.group(0)
        self.assertNotIn("delfolder", body,
                         "the Delete chip is back in the sidebar's folder list")
        self.assertNotRegex(body, r"Delete\s*[“\"]",
                            "a Delete label is being rendered among the folder chips")

    def test_the_folder_itself_carries_the_action(self):
        """Removing the chip without a replacement would just delete the feature."""
        bind = re.search(r"function _fxBindSide\(root\)\{.*?\n  \}", self.src, re.S)
        self.assertIsNotNone(bind, "_fxBindSide not found")
        body = bind.group(0)
        self.assertIn("oncontextmenu", body,
                      "no context-menu handler on the folder chips")
        self.assertIn("removeFolder", body,
                      "the folder context menu cannot actually remove a folder")

    def test_it_still_confirms_and_still_keeps_the_files(self):
        """A quieter delete must not become a quieter data loss."""
        bind = re.search(r"function _fxBindSide\(root\)\{.*?\n  \}", self.src, re.S).group(0)
        ctx = bind[bind.index("oncontextmenu"):]
        self.assertIn("uiConfirm", ctx, "the folder delete no longer asks")
        self.assertIn("move to All", ctx,
                      "the confirmation no longer says the files themselves survive")

    def test_music_is_not_offered_for_deletion(self):
        """Music is built in; offering to delete it is offering something that cannot happen."""
        bind = re.search(r"function _fxBindSide\(root\)\{.*?\n  \}", self.src, re.S).group(0)
        self.assertRegex(bind, r"name\s*===\s*'Music'\s*\)\s*return",
                         "the built-in Music folder is offered for deletion")


if __name__ == "__main__":
    unittest.main()
