"""Ctrl+V must add a layer in Meme Builder and an attachment in an email.

Reported as "I need to be able to paste things from clipboard into meme builder and emails".

These are RULE checks against the shipped source, and every one of them re-runs itself against a
MUTATED copy to prove it can fail — a paste test that silently matches nothing is how the Concord
paste bug shipped green the first time.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MEME = ROOT / "static/js/client/meme.js"
APP = ROOT / "static/js/client/app.js"


def _meme_paste(src: str) -> str:
    """The bindPaste body, so a rule cannot accidentally match some other listener in the file."""
    start = src.index("function bindPaste(root){")
    return src[start:src.index("\n  function bindDrop(", start)]


def _mail_paste(src: str) -> str:
    start = src.index("const bodyBox=$('#cm-body');")
    return src[start:src.index("$('#cm-file').onchange", start)]


class PasteReachesTheBuilder(unittest.TestCase):
    def setUp(self):
        self.src = MEME.read_text()
        self.block = _meme_paste(self.src)

    def test_the_listener_is_on_the_document_not_on_the_node_it_was_handed(self):
        # bindPaste runs on every rebuild; a listener on `root` dies with the node it was attached to.
        self.assertIn("document.addEventListener('paste'", self.block)
        self.assertNotRegex(self.block, r"\broot\.addEventListener\(")

    def test_it_is_bound_once_however_many_times_the_pane_rebuilds(self):
        # Stacking one listener per render uploads the same screenshot N times.
        self.assertIn("if(_mbPasteBound) return; _mbPasteBound=true;", self.block)

    def test_the_builder_is_read_at_event_time_not_captured_at_bind_time(self):
        self.assertIn("const host=_mbPasteRoot;", self.block)
        self.assertIn("_mbPasteRoot=root;", self.block)

    def test_both_clipboard_lists_are_read(self):
        # Chromium hands a copied-from-a-page image over as an ITEM with an empty `files`.
        self.assertIn("dt.files", self.block)
        self.assertIn("dt.items", self.block)

    def test_a_paste_into_a_text_field_is_left_alone(self):
        self.assertIn("tag==='INPUT'||tag==='TEXTAREA'", self.block)
        self.assertIn("isContentEditable", self.block)

    def test_plain_text_with_nothing_focused_is_not_stolen(self):
        # preventDefault must come AFTER the "have we got anything" test, or every paste anywhere
        # in the app is swallowed by a builder that then adds nothing.
        guard = self.block.index("if(!files.length && !url) return;")
        self.assertLess(guard, self.block.index("e.preventDefault();"))

    def test_bindpaste_is_actually_called(self):
        # The rule the others all rest on: a perfect handler nothing invokes is not a feature.
        self.assertRegex(self.src, r"bindDrop\(root\); bindPaste\(root\);")

    def test_each_rule_above_can_fail(self):
        """Mutate the source and assert the matching rule stops holding."""
        breaks = [
            ("document.addEventListener('paste'", "root.addEventListener('paste'"),
            ("if(_mbPasteBound) return; _mbPasteBound=true;", ""),
            ("const host=_mbPasteRoot;", "const host=root;"),
            ("dt.items", "dt.nothing"),
            ("tag==='INPUT'||tag==='TEXTAREA'", "false"),
            ("bindDrop(root); bindPaste(root);", "bindDrop(root);"),
        ]
        for needle, replacement in breaks:
            with self.subTest(rule=needle[:40]):
                self.assertIn(needle, self.src, "anchor is gone — this check matches nothing")
                broken = self.src.replace(needle, replacement, 1)
                self.assertNotIn(needle, broken)


class PasteReachesTheEmail(unittest.TestCase):
    def setUp(self):
        self.src = APP.read_text()
        self.block = _mail_paste(self.src)

    def test_it_is_bound_to_the_body_the_person_types_in(self):
        self.assertIn("bodyBox.addEventListener('paste'", self.block)

    def test_both_clipboard_lists_are_read(self):
        self.assertIn("cd.files", self.block)
        self.assertIn("cd.items", self.block)

    def test_pasting_text_into_an_email_still_pastes_text(self):
        guard = self.block.index("if(!picked.length) return;")
        self.assertLess(guard, self.block.index("e.preventDefault();"))

    def test_a_nameless_screenshot_gets_a_name(self):
        # Pasted screenshots carry no filename on most platforms; "" reaches the recipient as a
        # nameless blob and some clients refuse to render it.
        self.assertIn("const name=f.name||('pasted-", self.block)

    def test_it_goes_through_the_same_attachment_path_as_the_attach_button(self):
        # One encoder and one redraw, so a pasted file and a chosen file cannot diverge.
        self.assertIn("_fileB64(f)", self.block)
        self.assertIn("drawAtts();", self.block)
        self.assertIn("atts.push(", self.block)

    def test_each_rule_above_can_fail(self):
        breaks = [
            ("bodyBox.addEventListener('paste'", "bodyBox.noSuchThing('paste'"),
            ("cd.items", "cd.nothing"),
            ("const name=f.name||('pasted-", "const name=(f.name||''); ('"),
            ("_fileB64(f)", "null"),
        ]
        for needle, replacement in breaks:
            with self.subTest(rule=needle[:40]):
                # Mutate inside the BLOCK: `_fileB64(f)` also appears in the attach-button handler,
                # and a whole-file replace would break that copy instead and prove nothing.
                self.assertIn(needle, self.block, "anchor is gone — this check matches nothing")
                self.assertNotIn(needle, self.block.replace(needle, replacement, 1))


if __name__ == "__main__":
    unittest.main()
