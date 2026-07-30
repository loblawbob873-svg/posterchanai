#!/usr/bin/env python3
"""Static integrity checks for the /client icon sprite.

Every one of these encodes a defect that actually shipped (or nearly did) while converting the client's
emoji buttons to the sprite. They are all STATIC — no browser, no server — so this runs in a second and
belongs in front of any deploy that touches icons:

    venv-unified/bin/python scripts/check_client_icons.py

Exit code 0 = clean, 1 = something is wrong (details printed).

Why each check exists:

  missing-symbol      A <use> pointing at a symbol that does not exist renders NOTHING, at 0x0, with no
                      console error. This is the single most dangerous failure mode here because it is
                      invisible to every automated check that only looks for exceptions.
  interpolation       `${ICO('x','b-ic')}` only expands inside a template literal. In an 18k-line file
                      there is no cheap way to prove a given site is one — same-line backtick parity and
                      whole-file parity BOTH give wrong answers — so the rule is: emit literal markup.
                      A leaked `${ICO(` renders as visible source text on the page.
  variation-selector  Stripping an emoji that carried U+FE0E/U+FE0F leaves the selector behind. It is
                      invisible on most platforms and a tofu box on the rest.
  no-accessible-name  An aria-hidden <svg> contributes no accessible name. When the emoji WAS the label,
                      converting an icon-only button leaves it announcing nothing to a screen reader.
  inline-display      The icon gap is a flex `gap`. An inline style="display:…" cannot be overridden by
                      any stylesheet rule, and `gap` does nothing on a non-flex box, so the icon ends up
                      flush against the first letter. (display:none is fine — it is a hidden button.)
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPRITE = ROOT / "static/js/client/sprite.js"
SOURCES = [ROOT / "static/js/client/app.js",
           ROOT / "static/js/client/meme.js",
           ROOT / "templates/client.html"]

ICON_RE = re.compile(r'<svg class="ic [bx]-ic" aria-hidden="true"><use href="#i-[\w-]+"></use></svg>')
BUTTON_ICON_ONLY = re.compile(
    r'<button\b([^>]*)>(' + ICON_RE.pattern + r')\s*(?=</button>)')


def sprite_symbols():
    """The symbol ids defined in sprite.js (the sprite is a JSON-encoded string inside it)."""
    # The sprite is a backtick template literal in sprite.js — deliberately, so the SVG stays
    # readable and hand-editable rather than being a JSON blob nobody can add a symbol to.
    src = SPRITE.read_text()
    m = re.search(r"var SPRITE = `(.*?)`;", src, re.S)
    if not m:
        return None, "could not find the SPRITE template literal in sprite.js"
    return set(re.findall(r'<symbol id="i-([\w-]+)"', m.group(1))), None


def main():
    problems = []

    symbols, err = sprite_symbols()
    if err:
        print(f"FAIL  {err}")
        return 1

    referenced = set()
    for path in SOURCES:
        text = path.read_text()
        name = path.name

        referenced |= set(re.findall(r'href="#i-([\w-]+)"', text))

        # Only a LITERAL symbol name is a problem — `ICO(ic)` with a variable is genuinely dynamic
        # (the mobile More sheet builds its rows from a table) and cannot be static markup.
        for m in re.finditer(r"\$\{ICO\(\s*['\"]", text):
            line = text[:m.start()].count("\n") + 1
            problems.append(("interpolation", name, line,
                             "literal symbol name: emit <svg …><use …></svg>, not ${ICO('x')}"))

        for m in re.finditer(r"</use></svg>[︎️]", text):
            line = text[:m.start()].count("\n") + 1
            problems.append(("variation-selector", name, line,
                             "a stripped emoji left its U+FE0E/U+FE0F behind"))

        for m in BUTTON_ICON_ONLY.finditer(text):
            attrs = m.group(1)
            if "title=" not in attrs and "aria-label=" not in attrs:
                line = text[:m.start()].count("\n") + 1
                problems.append(("no-accessible-name", name, line,
                                 "icon-only button needs title= or aria-label="))

        for m in re.finditer(r'<button\b([^>]*)>(?=<svg class="ic b-ic")', text):
            attrs = m.group(1)
            bad = re.search(r'style="[^"]*display\s*:\s*(?!none)(\w[\w-]*)', attrs)
            if bad:
                line = text[:m.start()].count("\n") + 1
                problems.append(("inline-display", name, line,
                                 f"inline display:{bad.group(1)} defeats the flex icon gap"))

    # `#i-name` is the placeholder in the doc comments, not a real reference.
    missing = sorted(referenced - symbols - {"name"})
    for sym in missing:
        problems.append(("missing-symbol", "sprite.js", 0,
                         f"#i-{sym} is referenced but not defined — renders 0x0, silently"))

    print(f"sprite: {len(symbols)} symbols, {len(referenced)} referenced")
    if not problems:
        print("OK  all icon checks passed")
        return 0

    by_kind = {}
    for kind, f, line, msg in problems:
        by_kind.setdefault(kind, []).append((f, line, msg))
    for kind, rows in sorted(by_kind.items()):
        print(f"\nFAIL  {kind}  ({len(rows)})")
        for f, line, msg in rows[:20]:
            where = f"{f}:{line}" if line else f
            print(f"        {where}  {msg}")
        if len(rows) > 20:
            print(f"        … and {len(rows) - 20} more")
    return 1


if __name__ == "__main__":
    sys.exit(main())
