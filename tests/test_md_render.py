"""The client's markdown renderer, run as the SHIPPED code.

mdToHtml draws every README in the built-in Git browser, so "it does not render like GitHub" is a
real defect there rather than a cosmetic one. Two things it could not do, both hit by this repo's
own README:

  - GFM pipe tables. There was no table support at all, so a feature matrix rendered as a wall of
    literal |---|---| text.
  - CommonMark autolinks, <https://example.com>. The source is HTML-escaped before the inline pass,
    so an autolink arrives as &lt;…&gt; and the bare-URL rule swallowed the trailing &gt; into the
    href — a link to a URL with a stray > on the end.

The functions are extracted from app.js and run under node, so this tests what ships rather than a
copy that can drift. `enc` and `_mdUrl` are the two collaborators they need; both are stubbed with
the same contract app.js gives them (escape, and "return '' for anything not http(s)").
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "static" / "js" / "client" / "app.js"

STUBS = r"""
const enc = s => String(s == null ? '' : s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
  .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
const _mdUrl = u => /^https?:\/\//i.test(String(u||'')) ? String(u) : '';
"""


def _extract():
    src = APP.read_text()
    out = []
    for pat in (r"  const _mdIsDelim = .*?;\n",
                r"  function _mdCells\(row\)\{.*?\n  \}",
                r"  function mdInline\(s\)\{.*?\n  \}",
                r"  function mdToHtml\(src\)\{.*?\n  \}"):
        m = re.search(pat, src, re.S)
        assert m, f"not found in app.js: {pat}"
        out.append(m.group(0))
    return "\n".join(out)


def _render(md):
    script = STUBS + _extract() + "\nconsole.log(JSON.stringify(mdToHtml(" + json.dumps(md) + ")));\n"
    r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip())


pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def test_gfm_table_becomes_a_table():
    """The exact shape this repo's README uses: an EMPTY header row, then two-column rows."""
    html = _render("| | |\n|---|---|\n| Put it on a server | docs/NOSTR_DOCKER.md |\n"
                   "| Add the AI | docs/DOCKER.md |\n")
    assert "<table>" in html and "</table>" in html, f"no table rendered: {html[:200]}"
    assert html.count("<tr>") == 3, f"want a header row and two body rows: {html}"
    assert "Put it on a server" in html and "docs/DOCKER.md" in html
    assert "|---|" not in html, "the delimiter row leaked into the output"
    assert 'class="md-table"' in html, "a wide table must get its own scroll container"


def test_table_alignment_and_inline_markup_inside_cells():
    html = _render("| a | b | c |\n|:--|:-:|--:|\n| `code` | **bold** | [x](https://e.com) |\n")
    assert "text-align:left" in html and "text-align:center" in html and "text-align:right" in html
    assert "<code>code</code>" in html, "cells run through the inline pass"
    assert "<b>bold</b>" in html
    assert 'href="https://e.com"' in html


def test_a_paragraph_containing_pipes_is_not_a_table():
    """The delimiter row is what makes a table. Without it, pipes are just text."""
    html = _render("a | b | c\nnot a table\n")
    assert "<table>" not in html
    assert "<p>" in html


def test_autolink_does_not_swallow_its_closing_bracket():
    html = _render("Then open <http://localhost:3051/client> in a browser.\n")
    assert 'href="http://localhost:3051/client"' in html, f"autolink not linked: {html}"
    assert "&gt;</a>" not in html and 'client&gt;"' not in html, (
        "the trailing > was pulled into the link — that is the bug this guards")


def test_bare_url_still_links():
    html = _render("see https://example.com/x for more\n")
    assert 'href="https://example.com/x"' in html
