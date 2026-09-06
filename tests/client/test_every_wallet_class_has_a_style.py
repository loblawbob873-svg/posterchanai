"""THE WALLET SHIPPED AS TWENTY-ONE UNSTYLED DIV NAMES.

Reported as "the wallet looks like shit, did you even commit the css or create cSS?" — and no, I had
not. `exodus.js` drew 21 class names and the stylesheets defined NONE of them. It renders, it is
readable, every button works, and it looks like a browser default from 1997.

THIS IS THE SECOND TIME THIS REPO HAS RECORDED IT. `hostfiles.js` "used to draw eleven class names
of which the stylesheet defined *none*". A markup file and a stylesheet are edited separately and
nothing connects them, so the only thing that catches the gap is something that reads both.

Two further rules the wallet has to keep, and both are here because breaking them is invisible in
whatever theme the author happens to be using:

  * EVERY COLOUR IS A TOKEN. Nine themes redefine --neon/--panel/--line/--text; a hardcoded cyan
    looks right on `dark` and wrong on win98, professional, cherryblossom and monero.
  * THE LAYOUT IS QUERIED ON ITS CONTAINER, NOT THE VIEWPORT. `#feed` sits inside an `.osw` window
    on the windowed desktop, whose width has nothing to do with the screen — @media never fires
    there, which is the bug monero-wallet.css records having shipped.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "static/js/client/exodus.js"
CSS = ROOT / "static/css/exodus.css"


def _drawn():
    """Every `ex-*` class the module actually renders."""
    html = " ".join(re.findall(r'class="([^"]*)"', JS.read_text(encoding="utf-8")))
    return {c for c in html.split() if c.startswith("ex-")}


def test_the_stylesheet_exists_and_is_not_a_stub():
    assert CSS.exists(), "exodus.js draws ex-* classes and there is no stylesheet at all"
    assert len(CSS.read_text(encoding="utf-8")) > 1500


def test_every_class_the_wallet_draws_has_a_rule():
    css = CSS.read_text(encoding="utf-8")
    missing = sorted(c for c in _drawn() if f".{c}" not in css)
    assert not missing, f"drawn but never styled: {missing}"


def test_the_stylesheet_is_actually_loaded_and_cached():
    """A stylesheet nobody links is the same as no stylesheet."""
    assert "exodus.css" in (ROOT / "templates/client.html").read_text(encoding="utf-8")
    assert "'/static/css/exodus.css'" in (ROOT / "static/js/client/sw.js").read_text(encoding="utf-8")


def test_no_hardcoded_colour_outside_the_qr_quiet_zone():
    """A literal colour ignores the user's theme. The QR's white background is the one exception --
    it is read by a camera, and a dark quiet zone does not scan."""
    css = CSS.read_text(encoding="utf-8")
    # Strip comments before matching, or the prose about colours counts as a colour.
    body = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    literals = re.findall(r"#[0-9a-fA-F]{3,8}\b", body)
    assert set(literals) <= {"#fff"}, f"hardcoded colours ignore the theme: {sorted(set(literals))}"
    # rgba() is allowed only where it tints a THEME token (var(--accent-rgb)) or is a known accent
    # alpha; a bare literal triple would be the same bug in another notation.
    for triple in re.findall(r"rgba\(\s*([0-9]+\s*,\s*[0-9]+\s*,\s*[0-9]+)", body):
        assert triple in ("255,207,43", "214,31,78"), \
            f"rgba({triple}) is a literal colour; tint var(--accent-rgb) or use a token"


def test_the_layout_is_queried_on_its_container():
    """@media asks about the screen. This view lives in a window whose width is unrelated to it."""
    css = CSS.read_text(encoding="utf-8")
    assert "container:ex/inline-size" in css.replace(" ", "")
    assert "@container ex" in css
    # @media is allowed ONLY for things that really are facts about the screen.
    for query in re.findall(r"@media\s*\(([^)]*)\)", css):
        assert ("prefers-reduced-motion" in query or query.replace(" ", "") == "hover:hover"
                or "max-width:600px" in query.replace(" ", "")), query


def test_an_unreachable_balance_is_not_styled_like_a_number():
    """`unavailable` and a zero must not read the same. It is deliberately not tabular and not a
    digit -- the screen is the last place that distinction survives."""
    css = CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.ex-unknown\{([^}]*)\}", css)
    assert rule, ".ex-unknown has no rule"
    assert "dashed" in rule.group(1) and "var(--muted)" in rule.group(1)


def test_addresses_and_phrases_are_monospaced_and_break_anywhere():
    """An address that wraps mid-character in a proportional font is one somebody transcribes
    wrongly, and this is money."""
    css = CSS.read_text(encoding="utf-8")
    rule = re.search(r"\.ex-addr,\.ex-phrase\{([^}]*)\}", css)
    assert rule, "addresses have no rule"
    assert "var(--mono" in rule.group(1)
    assert "break-all" in rule.group(1) or "anywhere" in rule.group(1)
