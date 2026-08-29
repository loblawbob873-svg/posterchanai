import re
from pathlib import Path


CSS = (Path(__file__).resolve().parents[2] / "static" / "css" / "client.css").read_text()


def test_classic_desktop_social_has_a_real_scrollbar_without_changing_mobile():
    """The global reset is intentionally barless on touch; a fine-pointer desktop must override it.

    Check the complete media block rather than the mere presence of a thumb rule: Firefox uses
    scrollbar-width, Chromium uses the pseudo element, and an unscoped override regresses phones.
    """
    match = re.search(
        r"@media \(min-width:821px\) and \(hover:hover\) and \(pointer:fine\)\{"
        r"(?P<body>.*?)\n\}", CSS, re.S
    )
    assert match, "the desktop input/viewport gate is missing"
    body = match.group("body")
    selector = "body:not(.os-on) .app > .main > .feed"
    assert selector + "{" in body
    assert "scrollbar-width:thin" in body
    assert selector + "::-webkit-scrollbar{width:10px" in body
    assert selector + "::-webkit-scrollbar-thumb{" in body

    before = CSS[:match.start()]
    assert ".feed{flex:1;min-height:0;overflow-y:auto" in before
    assert ".feed::-webkit-scrollbar{width:0;height:0}" in before
    assert "@media(max-width:820px)" not in match.group(0)

