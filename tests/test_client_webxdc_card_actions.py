"""A mini app posted as kind 1063 is a POST, and a post you cannot answer is broken.

Run: venv-unified/bin/python -m unittest tests.test_client_webxdc_card_actions

`webxdcFileCard` renders a whole post whose subject is a mini app — the shape Ditto publishes and the
shape the Half-Life port arrived in. It was written by hand, with the avatar, the name, the time, the
Play cartridge and the caption… and no `.acts` row at all. So a game shared as a post could not be
replied to, reposted, quoted, reacted to, zapped or bookmarked, on any surface, by anybody. Nothing
errors and nothing is logged: the card looks finished, and the actions are simply absent.

The row is now ONE function (`actsRow`) shared with `noteCard`, so a card cannot be given a
different, quietly incomplete set of actions again. The delegated click handler keys on `data-a`
inside a `.note[data-id]` and nothing else, which is why sharing it is enough.

The two functions are lifted out of app.js — a 25k-line IIFE that cannot be imported — and run under
node against stubs. A grep for `class="acts"` would pass against a row missing half its buttons.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "static" / "js" / "client" / "app.js").read_text(encoding="utf-8")


def _func(name: str) -> str:
    """The whole of `function <name>(…){…}` from app.js, by brace balance."""
    m = re.search(r"^  function " + re.escape(name) + r"\(", APP, re.M)
    if not m:
        raise AssertionError(f"{name}() moved or was renamed — re-point this test")
    i = APP.index("{", m.end() - 1)
    depth, j = 0, i
    in_s = None
    while j < len(APP):
        c = APP[j]
        if in_s:
            if c == "\\":
                j += 2
                continue
            if c == in_s:
                in_s = None
        elif c in "'\"`":
            in_s = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return APP[m.start():j + 1]
        j += 1
    raise AssertionError(f"could not find the end of {name}()")


STUBS = """
const REPLY_ICON='<i r>', RT_ICON='<i t>', QUOTE_ICON='<i q>', REACT_ICON='<i k>', ZAP_ICON='<i z>';
const LOGO='/logo.png';
const BOOKMARKS = new Set();
const enc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const fmtSats = (n) => String(n);
const countsFor = () => ({ replies:2, reposts:1, reactions:3, zaps:0, tipN:0, iRt:false });
const myReaction = () => '';
const isXmrAddr = () => false, xmrForNote = () => '', isBchAddr = () => false, bchOf = () => '';
const profOf = () => ({ name:'Ann', picture:'' });
const needProfile = () => {};
const safePk = (pk) => pk.slice(0, 8);
const emojiName = (pk, n) => enc(n);
const timeAgo = () => '1m';
const applyEmojis = (h) => h;
const linkify = (t) => enc(t);
const tipCountLabel = () => '';
const webxdcCardHtml = () => '<div class="xdc-card">PLAY</div>';
"""


def _node(script):
    out = subprocess.run(["node", "-e", script], capture_output=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-2000:])
    return json.loads(out.stdout.decode() or "null")


def _render(fn_name):
    src = STUBS + _func("actsRow") + "\n" + _func("webxdcFileCard") + "\n"
    return _node(src + f"""
      const ev = {{ id:'e1', pubkey:'pk1', kind:1063, created_at:1, content:'a game',
                   tags:[['m','application/x-webxdc'],['url','https://h/a.xdc'],['webxdc','g1']] }};
      console.log(JSON.stringify({fn_name}(ev)));
    """)


ACTIONS = ["reply", "repost", "quote", "react", "tip", "menu"]


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class MiniAppPostsAreOrdinaryPosts(unittest.TestCase):

    def test_the_card_carries_the_full_action_row(self):
        html = _render("webxdcFileCard")
        self.assertIn('class="acts"', html, "a mini-app post has no actions at all")
        for a in ACTIONS:
            self.assertIn(f'data-a="{a}"', html, f"a mini-app post cannot be {a}'d")

    def test_the_row_sits_inside_the_note_the_click_handler_keys_on(self):
        """The handler finds the post by walking up to `.note[data-id]`. A row outside it is a set of
        buttons that do nothing."""
        html = _render("webxdcFileCard")
        self.assertRegex(html, r'<article class="note" data-id="e1"')
        self.assertLess(html.index('data-id="e1"'), html.index('class="acts"'))
        self.assertTrue(html.rstrip().endswith("</article>"))

    def test_the_app_itself_is_still_on_the_card(self):
        html = _render("webxdcFileCard")
        self.assertIn("xdc-card", html)
        self.assertIn("a game", html)

    def test_both_cards_render_the_SAME_row(self):
        """One implementation, because a hand-written second one is how this happened: the mini-app
        card looked finished and simply had fewer buttons than every other post."""
        for name in ("noteCard", "webxdcFileCard"):
            self.assertIn("actsRow(ev)", _func(name),
                          f"{name} builds its own action row — they will drift apart again")

    def test_the_android_back_button_can_close_a_full_screen_mini_app(self):
        """Every other overlay in the client registers in this chain (Notes' drawer, Web Search's
        reader, the vault drawer, the mini player). The mini-app sheet did not, so Back navigated the
        view UNDERNEATH it and left the game standing on top — on the one surface where Back is how
        anybody leaves a game."""
        chain = re.search(r"addListener\('backButton',\s*\(\)=>\{(.*?)\n        \}\);", APP, re.S)
        self.assertTrue(chain, "the backButton chain moved — re-point this test")
        body = chain.group(1)
        self.assertIn("PCWebxdc.sheetOpen", body)
        self.assertIn("PCWebxdc.closeSheet", body)
        # …before the fall-through that navigates the view, or it closes nothing.
        self.assertLess(body.index("PCWebxdc.sheetOpen"), body.index("history.back()"))


if __name__ == "__main__":
    unittest.main()
