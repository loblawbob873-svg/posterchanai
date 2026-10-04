"""A CSS selector is never built from page data without CSS.escape.

Two real bugs, one shape: `document.querySelector('#' + c.dataset.copy)`. A ✨ summary kept its whole
text in data-copy, so every click inside the sheet -- Copy and Close -- threw "Failed to execute
'querySelector' on 'Document'" (reported from Telegram); and Contacts' A–Z rail has a '#' group for names
that do not start with a letter, so tapping it looked up '#ct-l-#', which is not a selector at all, and
the jump silently never worked. Anything read off the page (dataset, an attribute, text) is data, and a
selector built from data must go through CSS.escape -- or not be a selector (getElementById).

This scans every shipped client script for a $ / $$ / querySelector(All) / closest call whose selector
argument concatenates or interpolates such a value without CSS.escape.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted((ROOT / "static/js/client").glob("*.js")) + sorted((ROOT / "static/js").glob("*.js"))
CALL = re.compile(r"(?:\bquerySelector(?:All)?|\bclosest|(?<![\w.])\${1,2})\(")
DATA = re.compile(r"\b(?:dataset|getAttribute|textContent|innerText)\b")
# Escaped for a selector: CSS.escape, or quotes stripped for an attribute-quoted [x="..."] selector.
SAFE = re.compile(r"CSS\.escape|\.replace\(/\[\"\\\\\]/g")


def _first_arg(src, start):
    """The text of the first argument of the call whose '(' is at src[start-1]."""
    depth, i, quote = 0, start, None
    end = src.find("\n", start)
    end = len(src) if end < 0 else end          # one line: a regex literal must not run the scan away
    while i < end:
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                return src[start:i]
            depth -= 1
        elif ch == "," and depth == 0:
            return src[start:i]
        i += 1
    return src[start:i]


def offenders():
    out = []
    for f in FILES:
        if f.name.endswith(".min.js") or "vendor" in f.name:
            continue
        src = f.read_text(errors="replace")
        for m in CALL.finditer(src):
            arg = _first_arg(src, m.end())
            built = "+" in arg or "${" in arg
            if built and DATA.search(arg) and not SAFE.search(arg):
                line = src.count("\n", 0, m.start()) + 1
                out.append(f"{f.relative_to(ROOT)}:{line}: {arg.strip()[:90]}")
    return out


def test_no_selector_is_built_from_page_data_unescaped():
    bad = offenders()
    assert not bad, "a selector built from page data without CSS.escape:\n  " + "\n  ".join(bad)


def test_the_scan_sees_the_shape_it_exists_for(tmp_path, monkeypatch):
    """Guard the guard: the exact lines that shipped broken are found."""
    f = tmp_path / "x.js"
    f.write_text("const el = $('#ct-l-' + b.dataset.l, root);\n"
                 "const t = document.querySelector('#'+c.dataset.copy);\n"
                 "const ok = $('#ct-l-' + CSS.escape(b.dataset.l), root);\n"
                 "const lit = root.querySelector('#'+id);\n")
    monkeypatch.setattr(sys.modules[__name__], "FILES", [f])
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    assert [o.split(":")[1] for o in offenders()] == ["1", "2"], offenders()
