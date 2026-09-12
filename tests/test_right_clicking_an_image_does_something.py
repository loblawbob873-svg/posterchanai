"""Right-clicking an image must offer something, and a dead menu must not open a window.

Run: venv-unified/bin/python -m unittest tests.test_right_clicking_an_image_does_something

Reported as: "right clicking on a post image causes other windows to appear and the right-click
menu does nothing! wtf is this shit!" — and both halves were the same omission.

Electron ships NO default context menu, so this shell builds one per event. It handled
`params.linkURL` and cut/copy/paste and nothing else, so on an IMAGE every item was disabled: an
image is not editable, so cut and paste are off, and it selects nothing, so copy is off. The menu
therefore contained only dead entries. On Wayland a native menu is a REAL WINDOW, so what the user
sees is a window appearing, containing nothing they can press.
"""
import re
import unittest
from pathlib import Path

MAIN = (Path(__file__).resolve().parents[1] / "desktop/main.js").read_text()
BLOCK = MAIN[MAIN.index("created.webContents.on('context-menu'"):]
BLOCK = BLOCK[:BLOCK.index("\n  });")]


class AnImageHasEntries(unittest.TestCase):
    def test_an_image_is_recognised_at_all(self):
        self.assertIn("params.mediaType === 'image'", BLOCK,
                      "the context menu still has no image case, so every entry is disabled on one")

    def test_the_image_itself_can_be_copied_and_saved(self):
        self.assertIn("copyImageAt", BLOCK)
        self.assertIn("downloadURL", BLOCK)

    def test_copying_the_image_does_not_go_through_writeImage(self):
        """`clipboard.writeImage` does not take the Wayland selection — the same reason the
        screenshot path verifies with `wl-paste` rather than trusting `readImage`. `copyImageAt`
        copies the image Chromium already decoded, which also avoids re-fetching a URL that may be
        an authenticated blob."""
        # Strip comments first: the reason NOT to use writeImage is written down beside the code,
        # and matching prose instead of code is how a check ends up asserting its own documentation.
        code = re.sub(r"/\*.*?\*/", "", BLOCK, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        self.assertNotIn("writeImage", code)

    def test_an_address_is_only_offered_when_there_is_one(self):
        """A data:/blob: source is this page's own memory. Offering to copy or open it would put a
        string on the clipboard that resolves nowhere."""
        self.assertIn("/^https?:/i.test(params.srcURL)", BLOCK)


class ADeadMenuNeverOpens(unittest.TestCase):
    def test_a_menu_with_nothing_clickable_is_not_shown(self):
        """THE HALF THAT PRODUCED "other windows appear". On Wayland this popup is a window, so a
        menu in which nothing can be pressed is indistinguishable from a bug."""
        self.assertIn("const usable = items.some", BLOCK)
        self.assertIn("if (!usable) return;", BLOCK)
        self.assertLess(BLOCK.index("if (!usable) return;"), BLOCK.index("Menu.buildFromTemplate"),
                        "the emptiness check runs after the menu is already built and shown")

    def test_separators_do_not_count_as_something_to_click(self):
        usable = BLOCK[BLOCK.index("const usable = items.some"):]
        usable = usable[:usable.index("\n")]
        self.assertIn("i.type !== 'separator'", usable,
                      "a menu of nothing but separators would count as usable and open a window")
        self.assertIn("i.enabled !== false", usable,
                      "disabled entries count as usable, which is exactly the reported bug")


if __name__ == "__main__":
    unittest.main()
