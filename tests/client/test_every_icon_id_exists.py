"""AN ICON THE SPRITE DOES NOT DEFINE DRAWS NOTHING, AND SAYS NOTHING.

Reported as "the taskbar icon for office stuff is empty, nothing". The document window asked for
`i-doc`; the sprite has 141 icons and has never had that one. `<use href="#i-doc">` is not an
error — SVG resolves the reference to nothing and renders empty space. No console message, no
missing-image glyph, nothing in any log. The button is there, the right size, and blank.

That is a whole class of bug rather than one typo, so this checks the property: every icon id the
client actually references exists. It is the same shape as the stylesheet check in the office
tests — markup naming something nobody defined.

COMMENTS ARE STRIPPED FIRST, and that is load-bearing: two of the three ids this found on its first
run (`i-name`, `i-window`) appear only inside comments explaining this exact failure, one of them in
the note left behind the last time somebody paid for it. A check that reads those as references
fails forever on prose and gets deleted.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPRITE = ROOT / "static/js/client/sprite.js"

#: `<use href="#i-x">` is the only form that reaches the renderer. A bare 'i-x' string is an
#: argument that may never be drawn, so it is deliberately not scanned here.
USE = re.compile(r'href="#(i-[a-z0-9-]+)"')


def defined() -> set[str]:
    return set(re.findall(r'id="(i-[a-z0-9-]+)"', SPRITE.read_text(encoding="utf-8")))


def _strip_comments(text: str, html: bool) -> str:
    if html:
        return re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    # Line comments only where the line has no quote before them — enough to drop prose without
    # mangling a URL like https:// inside a string.
    return "\n".join(re.sub(r"(^|\s)//.*$", " ", line) if '"' not in line and "'" not in line
                     else line for line in text.splitlines())


def sources():
    for path in sorted(ROOT.glob("static/js/client/*.js")):
        if path.name != "sprite.js":
            yield path, False
    for path in sorted(ROOT.glob("templates/**/*.html")):
        yield path, True


def test_the_sprite_still_parses_as_a_set_of_ids():
    """The check before the check: an empty set makes every assertion below vacuous."""
    have = defined()
    assert len(have) > 100, f"only {len(have)} icons found — the sprite's shape has changed"
    assert "i-note" in have


@pytest.mark.parametrize("relpath", [str(p.relative_to(ROOT)) for p, _ in sources()])
def test_every_referenced_icon_is_defined(relpath):
    path = ROOT / relpath
    body = _strip_comments(path.read_text(encoding="utf-8"), relpath.endswith(".html"))
    missing = sorted(set(USE.findall(body)) - defined())
    assert not missing, (
        f"{relpath} draws {', '.join('#' + m for m in missing)}, which the sprite does not "
        f"define — that renders as empty space with no error anywhere")


def test_the_office_window_icon_in_particular():
    """The reported one, by name, so the fix cannot be silently reverted."""
    app = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
    call = re.search(r"PCOS\.openDoc\('office:'\+session\.id,\s*file\.name,\s*'([^']+)'", app)
    assert call and call.group(1) in defined()
