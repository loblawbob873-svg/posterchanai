#!/usr/bin/env python3
"""Which BUTTONS of the client no test ever names, read from the source (2026-10-22: "every feature needs test
coverage to prevent stupid experiences for the user").

feature_coverage.py asks whether each SCREEN is driven by a browser test. A screen can be, and still have a dozen
buttons nobody ever pressed in a test. This collects every <button id="..."> in static/js/client/*.js and checks
whether any test or check names that id. It is a floor, not proof (a test may name an id without pressing it), and
it is enforced as a RATCHET by tests/test_every_control_is_named_by_a_test.py: the untested count may only go down,
so a new button without a test fails the gate at once.

    venv-unified/bin/python scripts/control_coverage.py            # per module, worst first
    venv-unified/bin/python scripts/control_coverage.py meme.js    # the untested ids of one module
"""
from __future__ import annotations

import collections
import glob
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# AI is out of scope for now (the owner, 2026-10-22: "not AI at this time").
EXCLUDED_MODULES = {"ai.js"}
_BUTTON = re.compile(r'<button[^>]*\bid="([a-zA-Z][\w-]*)"')


def buttons() -> dict:
    """{id: {module, ...}} for every literal button id in the client."""
    out = collections.defaultdict(set)
    for f in sorted(glob.glob(str(ROOT / "static/js/client/*.js"))):
        name = Path(f).name
        if name in EXCLUDED_MODULES:
            continue
        for i in _BUTTON.findall(Path(f).read_text(errors="replace")):
            out[i].add(name)
    return dict(out)


def test_tokens() -> set:
    tok = set()
    for pat in ("tests/**/*.py", "tests/**/*.mjs", "tests/**/*.cjs", "scripts/check_*.py"):
        for f in glob.glob(str(ROOT / pat), recursive=True):
            tok.update(re.findall(r"[\w-]+", Path(f).read_text(errors="replace")))
    return tok


def untested() -> dict:
    """{module: sorted [ids no test names]}"""
    tok, out = test_tokens(), collections.defaultdict(list)
    for i, mods in buttons().items():
        if i not in tok:
            for m in mods:
                out[m].append(i)
    return {m: sorted(v) for m, v in out.items()}


def main(argv) -> int:
    u, total = untested(), len(buttons())
    if len(argv) > 1:
        for i in u.get(argv[1], []):
            print(i)
        return 0
    n = len({i for v in u.values() for i in v})
    for m, ids in sorted(u.items(), key=lambda kv: -len(kv[1])):
        print(f"{m:28} {len(ids):3} untested")
    print(f"\n{n} of {total} button ids are named by no test (AI excluded)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
