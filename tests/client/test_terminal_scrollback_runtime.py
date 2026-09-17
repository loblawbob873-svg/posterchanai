"""Reading terminal scrollback survives a resize and a scrollbar drag. Runs the SHIPPED helpers from
static/js/client/term.js under node against a stub xterm buffer.

  * a resize while scrolled up keeps the same number of rows between the viewport and the bottom
    (the old viewport index lands somewhere else after a reflow);
  * dragging the scrollbar up stops following the live prompt; a plain upward layout scroll without a
    held bar press does not.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

TERM = (Path(__file__).parents[2] / "static/js/client/term.js").read_text(encoding="utf-8")


def _function(name):
    start = TERM.index(f"function {name}(")
    brace = TERM.index("{", start)
    depth, quote, escaped = 0, None, False
    for pos in range(brace, len(TERM)):
        ch = TERM[pos]
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in "'\"`":
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return TERM[start:pos + 1]
    raise AssertionError(name)


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_resize_keeps_rows_above_bottom_and_bar_drag_stops_following():
    script = f"""
let followBottom=true, scrollingByUs=false, bottomPinEpoch=0, bottomPinT=null, _barDrag=false;
const buf={{baseY:500, viewportY:440}};
const scrolled=[];
const term={{buffer:{{active:buf}}, scrollToLine:n=>{{scrolled.push(n); buf.viewportY=n;}}}};
{_function('_stopFollowing')}
{_function('_rowsAboveBottom')}
{_function('_keepRowsAboveBottom')}
{_function('_scrollFromBar')}
const out={{}};
// resize while reading history: 60 rows above the bottom; reflow grows the buffer to 800
const keep=_rowsAboveBottom(); buf.baseY=800; buf.viewportY=440; _keepRowsAboveBottom(keep);
out.keep=keep; out.after=buf.baseY-buf.viewportY; out.scrolled=scrolled.slice();
// no measurement -> nothing moves
_keepRowsAboveBottom(null); out.nullMoves=scrolled.length;
// upward scroll without a bar press is not the person's
followBottom=true; out.noBar=_scrollFromBar(700); out.followNoBar=followBottom;
// with the bar held, an upward scroll stops following
_barDrag=true; out.bar=_scrollFromBar(700); out.followBar=followBottom;
// at the bottom with the bar held: not a scroll away
followBottom=true; out.barBottom=_scrollFromBar(800); out.followBarBottom=followBottom;
process.stdout.write(JSON.stringify(out));
"""
    got = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert got["keep"] == 60 and got["after"] == 60 and got["scrolled"] == [740]
    assert got["nullMoves"] == 1
    assert got["noBar"] is False and got["followNoBar"] is True
    assert got["bar"] is True and got["followBar"] is False
    assert got["barBottom"] is False and got["followBarBottom"] is True


def test_the_helpers_are_wired_into_fit_scroll_and_pointer_paths():
    assert "const keepAbove=followThisFit ? null : _rowsAboveBottom();" in TERM
    assert "else _keepRowsAboveBottom(keepAbove);" in TERM
    assert TERM.index("if(_scrollFromBar(y))return;") < TERM.index("if(scrollingByUs)return;", TERM.index("term.onScroll((y) =>"))
    assert "classList.contains('xterm-viewport')) _barDrag = true;" in TERM
    assert "_barUp: () => { _barDrag = false; }" in TERM
