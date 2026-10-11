"""THE CLIENT'S BUTTONS ARE ICONS, NOT TEXT CHARACTERS THAT A FONT MAY NOT HAVE.

The channel options, message actions and header "more" buttons were the character U+22EF (⋯), and the
star toggle U+2606/U+2605 (☆/★). Neither is in Inter or Orbitron, so they render only where some
fallback font has them: on server1's headless Chrome every one drew as an empty box, and PosterChanOS
ships a slim font set with the same gap. The same was true of the composer, timeline composer, This
Computer and music-share More buttons and of three toasts/help lines, so every client file is checked. The client already has an icon sprite for exactly this.

Checked on the source with comments removed, plus the sprite defining every icon these buttons use.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "static/js/client").glob("*.js")
               if not p.name.endswith(".min.js") and not p.name.startswith(("cord-protocol", "cord-reader")))
GLYPHS = "⋯☆★"


def _code(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", text)


def test_no_client_button_or_message_is_a_bare_glyph():
    bad = []
    for rel in FILES:
        code = _code((ROOT / rel).read_text(encoding="utf-8"))
        for g in GLYPHS:
            for m in re.finditer(re.escape(g), code):
                bad.append((rel, g, code[max(0, m.start() - 60):m.end() + 10]))
    assert not bad, ("buttons drawn with a text glyph some fonts lack -- use the sprite", bad)


def test_the_sprite_has_every_icon_the_client_uses():
    sprite = (ROOT / "static/js/client/sprite.js").read_text(encoding="utf-8")
    defined = set(re.findall(r'<symbol id="(i-[a-z0-9-]+)"', sprite))
    used = set()
    for rel in FILES:
        used |= set(re.findall(r'#(i-[a-z0-9-]+)["\']', _code((ROOT / rel).read_text(encoding="utf-8"))))
    missing = sorted(used - defined)
    assert not missing, ("the client uses icons the sprite does not define (they draw as nothing)", missing)
