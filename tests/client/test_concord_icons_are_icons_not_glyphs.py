"""CONCORD'S BUTTONS ARE ICONS, NOT TEXT CHARACTERS THAT A FONT MAY NOT HAVE.

The channel options, message actions and header "more" buttons were the character U+22EF (⋯), and the
star toggle U+2606/U+2605 (☆/★). Neither is in Inter or Orbitron, so they render only where some
fallback font has them: on server1's headless Chrome every one drew as an empty box, and PosterChanOS
ships a slim font set with the same gap. The client already has an icon sprite for exactly this.

Checked on the source with comments removed, plus the sprite defining every icon these buttons use.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = ["static/js/client/concord.js", "static/js/client/cord-spaces.js"]
GLYPHS = "⋯☆★"


def _code(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def test_no_concord_button_is_a_bare_glyph():
    bad = []
    for rel in FILES:
        code = _code((ROOT / rel).read_text(encoding="utf-8"))
        for g in GLYPHS:
            for m in re.finditer(re.escape(g), code):
                bad.append((rel, g, code[max(0, m.start() - 60):m.end() + 10]))
    assert not bad, ("buttons drawn with a text glyph some fonts lack -- use the sprite", bad)


def test_the_sprite_has_every_icon_concord_uses():
    sprite = (ROOT / "static/js/client/sprite.js").read_text(encoding="utf-8")
    defined = set(re.findall(r'<symbol id="(i-[a-z0-9-]+)"', sprite))
    used = set()
    for rel in FILES:
        used |= set(re.findall(r'#(i-[a-z0-9-]+)"', (ROOT / rel).read_text(encoding="utf-8")))
    missing = sorted(used - defined)
    assert not missing, ("Concord uses icons the sprite does not define (they draw as nothing)", missing)
