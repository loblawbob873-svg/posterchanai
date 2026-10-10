"""No <label> may wrap a switch together with another control.

A click inside a <label> activates the label's FIRST control. Two rows here were labels holding a switch AND
something before it -- the startup-apps row (a monitor <select>: an enabled app could never be switched off) and
the Settings -> Sidebar row (its Move-up button: clicking a row's name moved it). The switch must be its own label
inside a plain container. This scans every shipped client template, so a new row cannot bring the shape back.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTROL = re.compile(r'<(select|button|textarea)\b|<input\b(?![^>]*type="hidden")', re.I)


def _outer_labels(src):
    """Top-level <label>...</label> spans, nesting-aware."""
    out, depth, start = [], 0, None
    for m in re.finditer(r'<label\b|</label>', src):
        if m.group(0).startswith('<label'):
            if depth == 0:
                start = m.start()
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0:
                out.append((start, m.end()))
    return out


def test_no_label_holds_a_switch_and_another_control():
    bad = []
    for f in sorted((ROOT / 'static/js/client').glob('*.js')):
        src = f.read_text(encoding='utf-8', errors='replace')
        for a, b in _outer_labels(src):
            body = src[a:b]
            if 'class="switch"' in body[6:] or 'class="slider"' in body:
                if len(CONTROL.findall(body)) >= 2:
                    bad.append('%s:%d %s' % (f.name, src[:a].count('\n') + 1, re.sub(r'\s+', ' ', body)[:120]))
    assert not bad, "a <label> wraps a switch and another control (a click goes to the first one):\n" + "\n".join(bad)
