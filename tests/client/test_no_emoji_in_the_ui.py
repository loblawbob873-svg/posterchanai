"""The UI draws icons from the sprite, not glyphs from whatever font the platform happens to have.

"i open email app and none of the bottom buttons have icons?"

The mail action row was `🗄 Move`, `🗑 Delete`, `↪ Forward`, `↩↩ Reply all` — characters, on a screen
that ships an icon set precisely so the interface does not depend on a font. A platform without the
emoji font (a minimal Gentoo install, a WebView, a locked-down phone) draws them as nothing at all,
and `↩↩` is two characters pretending to be one symbol even where it does render.

This is the same failure one layer down as an icon named but not DEFINED, which renders as blank
space with no error and no console entry. So both are checked here: the row uses the sprite, and
every symbol it names exists.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "static" / "js" / "client" / "app.js"
SPRITE = ROOT / "static" / "js" / "client" / "sprite.js"

# Pictographs and dingbats — deliberately NOT every non-ASCII character. Typographic marks the app
# uses on purpose (— … ▾ ✓ ·) are text, they come from the same font as the label beside them, and
# banning them would be a test about punctuation.
EMOJI = re.compile("[\U0001F300-\U0001FAFF←-⇿☀-➿]")


class MailActionsUseTheSprite(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = APP.read_text()
        cls.sprite = SPRITE.read_text()
        i = cls.src.index('<div class="mail-actions">')
        cls.row = cls.src[i:cls.src.index("</div>", i)]

    def test_no_pictographs_in_the_action_row(self):
        found = sorted(set(EMOJI.findall(self.row)))
        self.assertEqual(found, [],
                         "the mail actions draw %r from the font — a platform without it shows "
                         "nothing at all" % found)

    def test_every_button_carries_a_sprite_icon(self):
        buttons = re.findall(r'<button[^>]*data-act="([a-z]+)"[^>]*>(.*?)</button>', self.row, re.S)
        self.assertTrue(buttons, "the action row changed shape — re-read this test")
        for act, body in buttons:
            with self.subTest(action=act):
                self.assertIn("#i-", body, "the %s button has no icon" % act)

    def test_open_message_actions_are_icon_only_but_accessible(self):
        buttons = re.findall(r'(<button[^>]*data-act="([a-z]+)"[^>]*>)(.*?)</button>', self.row, re.S)
        for opening, act, body in buttons:
            with self.subTest(action=act):
                self.assertIn("icon-only", opening)
                self.assertRegex(opening, r'aria-label="[^"]+"')
                self.assertEqual(re.sub(r'<[^>]+>', '', body).strip(), '')

    def test_every_icon_it_names_is_defined(self):
        """An icon named but not in the sprite renders as blank space with no error — which is
        indistinguishable from the bug this test exists for."""
        for name in sorted(set(re.findall(r"#i-([a-z0-9-]+)", self.row))):
            with self.subTest(icon=name):
                self.assertIn('id="i-%s"' % name, self.sprite,
                              "the mail row names icon '%s', which the sprite does not define" % name)

    def test_the_row_still_offers_every_action(self):
        """check_mail_mobile.py pins the button COUNT, and new AI actions belong in the menu rather
        than the row. Restyling must not quietly drop one."""
        acts = re.findall(r'data-act="([a-z]+)"', self.row)
        self.assertEqual(sorted(acts),
                         sorted(["reply", "replyall", "forward", "ai", "unread", "move", "delete"]))

    def test_forward_is_an_email_arrow_not_the_generic_share_graph(self):
        button = re.search(r'<button[^>]*data-act="forward"[^>]*>(.*?)</button>', self.row, re.S)
        self.assertIsNotNone(button)
        self.assertIn("#i-forward", button.group(1))
        self.assertNotIn("#i-share", button.group(1))


if __name__ == "__main__":
    unittest.main()
