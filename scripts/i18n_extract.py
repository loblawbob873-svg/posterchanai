#!/usr/bin/env python3
"""Pull this client's user-facing English strings out of source into a translation catalogue.

    venv-unified/bin/python scripts/i18n_extract.py            # write static/i18n/en.json
    venv-unified/bin/python scripts/i18n_extract.py --stats    # just say how many, and from where

WHY A SCRIPT AND NOT A HAND-KEPT FILE. The catalogue has to be REGENERABLE, because the alternative
is a list that silently falls behind the app: a screen added next month is untranslated and nothing
anywhere says so. Re-running this and diffing is what turns "is the Arabic complete?" into a
question with an answer.

WHAT COUNTS AS A UI STRING. The client writes its interface as HTML inside template literals, so the
high-signal shapes are text between tags, the handful of human-readable attributes, and toast()
calls. Everything here is a heuristic, so the rules are biased towards FALSE NEGATIVES: a string
this misses is left in English, which is the graceful failure, while a string it wrongly includes is
a translated fragment of somebody's data, which is not. Hence the filters below — no strings
carrying markup or interpolation, nothing that looks like a class name, an id, a URL or a format
specifier, and nothing under three characters.

Interpolated strings are dropped ON PURPOSE. `Deleted ${n} files` cannot be a catalogue key (every n
is a different key) and cannot be reassembled by the DOM layer, which sees one text node. Those need
a call-site t() with placeholders, which is a separate and much smaller job than the bulk of the UI.
"""
from __future__ import annotations

import argparse
import html
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT_JS = ROOT / "static" / "js" / "client"
TEMPLATES = ROOT / "templates"
OUT = ROOT / "static" / "i18n" / "en.json"

# Files that render no interface. sw.js is a service worker, the rest are pure logic//parsers whose
# strings are protocol, not prose.
SKIP_FILES = {"sw.js", "negentropy.js", "zip.js", "qr.js", "ical.js", "vcard.js", "i18n.js"}

# Text sitting between two tags inside a template literal or an HTML file.
BETWEEN_TAGS = re.compile(r">([^<>{}`$]{2,120})<")
# The attributes a person actually reads.
ATTR = re.compile(r"""\b(?:placeholder|title|aria-label|alt|data-label)\s*=\s*["']([^"'`${}]{2,120})["']""")
# toast('…') / toast("…") — the app's own notifications.
TOAST = re.compile(r"""\btoast\(\s*["']([^"'`${}]{2,160})["']""")

# Rejects. Each of these was a real false positive on a first run over this repo.
REJECT = re.compile(
    r"""(?x)
    ^\s*$                      # blank
    | ^[\W\d_]+$               # punctuation / numbers / emoji only — same in every language
    | ^[a-z0-9_-]+$            # a bare token: class name, id, slug, enum value
    | ^\#?[0-9a-fA-F]{3,8}$    # a colour
    | https?://                # a URL
    | ^[/.]                    # a path
    | ^\d+(\.\d+)?\s*(px|em|rem|%|s|ms|kb|mb|gb)$   # a measurement
    | %[sd]                    # a format specifier
    | ^&[a-z]+;$               # a bare entity
    """
)


# CODE, caught by the `>…<` rule reading a comparison. `if(cx >= s.left && cy < s.bottom)` contains
# a perfectly good ">…<" and the text between it is JavaScript. This is the single largest source of
# false positives and the one that matters most: a translated regex fragment is a broken app, where
# a missed label is only an English label.
CODE = re.compile(
    r"""(?x)
    ===|!==|&&|\|\||=>|\+\+|--|\}\s*;|\)\s*;|\)\s*\{      # operators and statement punctuation
    | ^\s*[=<>+*/|&!]                                      # opens with an operator
    | [=<>+*/|&!]\s*$                                      # ends with one
    | \\[nst/]                                             # an escape sequence: regex or JS literal
    # A call — enc(err), _fxSideHTML(). The lookahead spares the pluralisation idiom, which is
    # ordinary UI English and looks identical to a call: "Download image model(s)", "file(s)".
    | \w+\((?!s\))
    | \w+\s*\[                                             # an index
    | ['"]\s*[+?:]                                         # a quote meeting concatenation/ternary
    | [+?:]\s*['"]                                         # …and the other way round
    """
)


def looks_like_prose(s: str) -> bool:
    s = s.strip()
    if len(s) < 3 or len(s) > 160:
        return False
    if REJECT.search(s) or CODE.search(s):
        return False
    # A multi-line run carrying statement punctuation is a block of code, not a wrapped sentence.
    if "\n" in s and re.search(r"[;{}]", s):
        return False
    # At least one run of two letters — filters out "1)" , "→" , "· ·" and friends.
    if not re.search(r"[A-Za-z]{2}", s):
        return False
    # A camelCase or dotted identifier that slipped through as "someThing.other".
    if re.fullmatch(r"[A-Za-z]+(\.[A-Za-z]+)+", s):
        return False
    return True


def harvest(text: str) -> list[str]:
    """Strings as the DOM will hold them, which means DECODED.

    The source says `Save &amp; reload`; by the time that text node exists the browser has parsed it
    into `Save & reload`, and the runtime layer matches against the node. Extracting the raw form
    gives a catalogue key that can never match anything on screen — a translation that is present,
    correct, and silently never applied, which is the worst of the three possible outcomes. Same for
    the `&nbsp;` in every size label and the `&#10;` in multi-line placeholders.
    """
    out: list[str] = []
    for rx in (BETWEEN_TAGS, ATTR, TOAST):
        for m in rx.finditer(text):
            s = html.unescape(m.group(1)).strip()
            if looks_like_prose(s):
                out.append(s)
    return out


def sources() -> list[Path]:
    files = [p for p in sorted(CLIENT_JS.glob("*.js")) if p.name not in SKIP_FILES]
    files += sorted(TEMPLATES.glob("*.html"))
    files += sorted(TEMPLATES.glob("includes/**/*.html"))
    files += sorted(TEMPLATES.glob("admin/**/*.html"))
    return files


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true", help="report counts instead of writing")
    args = ap.parse_args()

    per_file: Counter[str] = Counter()
    strings: set[str] = set()
    for p in sources():
        try:
            found = harvest(p.read_text(errors="ignore"))
        except OSError:
            continue
        if found:
            per_file[str(p.relative_to(ROOT))] = len(set(found))
            strings.update(found)

    ordered = sorted(strings, key=lambda s: (s.lower(), s))
    if args.stats:
        print(f"{len(ordered)} unique strings from {len(per_file)} files\n")
        for name, n in per_file.most_common(20):
            print(f"{n:6d}  {name}")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # English is the identity mapping and is never loaded at runtime (see i18n.js). It is written
    # anyway because it IS the catalogue: it is what a translator translates and what a later run
    # diffs against to find what a new feature added.
    OUT.write_text(json.dumps({s: s for s in ordered}, ensure_ascii=False, indent=1) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)} — {len(ordered)} strings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
