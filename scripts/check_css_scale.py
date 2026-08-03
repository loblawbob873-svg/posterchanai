#!/usr/bin/env python3
"""Guard the client's spacing/type scale — run before deploying a CSS change.

static/css/client.css was swept onto a design scale once (31 font sizes -> 8, 39 spacing values -> a
2px grid, 17 line-heights -> 3, 7 monospace stacks -> 1). Nothing keeps it there. A single hand-added
`font-size:12.5px` reads fine in review and silently starts the drift back, because the symptom is not
a broken page — it is a page where nothing quite lines up, which no one files a bug about.

    venv-unified/bin/python scripts/check_css_scale.py

Exit 0 = on-scale, 1 = drift (offenders printed with line numbers).

Same exemptions as the original sweep, and for the same reasons:
  * comments are not code
  * NEGATIVE lengths cancel a border or a deliberate overlap; snapping one breaks the alignment it
    exists to create
  * lengths inside calc()/clamp()/env()/min()/max() are computed, not scale steps
  * 1px padding/margin is an optical nudge
  * line-height:1 centres a glyph in an icon button, and line-height:0 removes the descender gap
    under an inline-block <img> wrapper — both are layout, not typography
"""
import re
import sys
from pathlib import Path

CSS = Path(__file__).resolve().parent.parent / "static" / "css" / "client.css"

FONT_LADDER = {11, 12, 13, 15, 17, 20, 24, 30}
# One-off display glyphs: hierarchy up here comes from the gap to the step below, not from a grid.
FONT_DISPLAY = {34, 36, 40, 42, 58}
# 0 collapses the line box under an inline-block <img> (kills the descender gap); 1 centres a glyph
# in an icon button. Both are layout idioms that a typographic scale has no opinion about.
LINE_HEIGHTS = {0, 1, 1.2, 1.45, 1.6}

SPACE_PROPS = (
    "padding", "margin", "gap", "row-gap", "column-gap",
)
SKIP_FN = re.compile(r"\b(calc|var|min|max|clamp|env)\s*\(")


def code_segments(css):
    """Yield (offset, text) for every non-comment run."""
    i = 0
    for m in re.finditer(r"/\*.*?\*/", css, re.S):
        if m.start() > i:
            yield i, css[i:m.start()]
        i = m.end()
    if i < len(css):
        yield i, css[i:]


def line_of(css, off):
    return css.count("\n", 0, off) + 1


def main():
    css = CSS.read_text(encoding="utf-8")
    bad = []

    prop_re = re.compile(
        r"(?<![-\w])(" + "|".join(SPACE_PROPS) + r")(?:-(?:top|right|bottom|left|inline|block)"
        r"(?:-(?:start|end))?)?(\s*:\s*)([^;{}]*)")
    font_re = re.compile(r"(?<![-\w])font-size(\s*:\s*)([^;{}]*)")
    lh_re = re.compile(r"(?<![-\w])line-height(\s*:\s*)([^;{}]*)")
    num = re.compile(r"(?<![\w.#-])(\d+(?:\.\d+)?)px\b")

    for base, seg in code_segments(css):
        for m in font_re.finditer(seg):
            val = m.group(2)
            if SKIP_FN.search(val):
                continue
            for nm in num.finditer(val):
                if nm.start(1) > 0 and val[nm.start(1) - 1] == "-":
                    continue
                n = float(nm.group(1))
                if n not in FONT_LADDER and n not in FONT_DISPLAY:
                    bad.append((line_of(css, base + m.start()), "font-size",
                                f"{nm.group(1)}px is off the ladder {sorted(FONT_LADDER)}"))

        for m in lh_re.finditer(seg):
            val = m.group(2).strip()
            if SKIP_FN.search(val) or re.search(r"(px|em|%|rem)", val):
                continue          # a length line-height is doing layout
            try:
                n = float(val)
            except ValueError:
                continue
            if n not in LINE_HEIGHTS:
                bad.append((line_of(css, base + m.start()), "line-height",
                            f"{val} is not one of {sorted(LINE_HEIGHTS)}"))

        for m in prop_re.finditer(seg):
            val = m.group(3)
            if SKIP_FN.search(val):
                continue
            for nm in num.finditer(val):
                if nm.start(1) > 0 and val[nm.start(1) - 1] == "-":
                    continue      # negative: exempt
                n = float(nm.group(1))
                if n <= 1:
                    continue      # 0 and 1px: exempt
                if n != int(n) or int(n) % 2:
                    bad.append((line_of(css, base + m.start()), m.group(1),
                                f"{nm.group(1)}px is not on the even 2px grid"))

    stacks = set(re.findall(r"font-family:\s*([^;{}]*mono[^;{}]*)", css))
    for st in sorted(stacks):
        if st.strip() != "var(--mono)":
            bad.append((0, "font-family", f"monospace stack {st!r} should be var(--mono)"))
    if "--mono:" not in css:
        bad.append((0, "font-family", "--mono is referenced but never defined"))

    if not bad:
        print(f"OK  {CSS.name} is on scale "
              f"(type {sorted(FONT_LADDER)}, spacing even-2px, line-height {sorted(LINE_HEIGHTS)})")
        return 0
    for line, prop, msg in sorted(bad):
        print(f"  {CSS.name}:{line}  {prop}: {msg}")
    print(f"\n{len(bad)} off-scale value(s). Snap to the nearest step, or add a documented exemption.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
