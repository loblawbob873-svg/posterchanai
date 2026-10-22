#!/usr/bin/env python3
"""Which screens of the client are exercised by a BROWSER test, read from the source (2026-10-22: "every feature
needs test coverage to prevent stupid experiences for the user").

Every screen already has a SMOKE test -- scripts/check_every_view_opens.py opens each one in the gate and fails on a
JavaScript error. That proves a screen draws, not that it works: Telegram drew and sat on "CONNECTING…", Communities
drew and loaded no rooms. This answers the next question per screen: which browser tests actually drive it?

The screens are DISCOVERED (every `data-view` in the client shell, plus the screens reached another way), and so are
the tests (anything that starts Chrome). A screen counts as covered when a browser test reaches it -- by switchView,
the sidebar button, the Messages tabs, or the function that opens it. tests/test_every_feature_has_a_test.py turns
this into a rule: a screen with no browser test must be on KNOWN_GAPS with a reason, and the list can only shrink.

    venv-unified/bin/python scripts/feature_coverage.py          # the table, gaps first
"""
from __future__ import annotations

import glob
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# AI screens are out of scope for now (the owner, 2026-10-22: "not AI at this time").
AI_VIEWS = {"ai", "translate"}

# Screens opened by something other than switchView/the sidebar -- each with the call that opens it. ("Discover" is
# a sidebar GROUP of other screens, not a screen, so it is not here.)
EXTRA = {
    "profile": [r"openProfile\(", r"renderProfileView\("],
    "thread": [r"openThread\(", r"renderThread\("],
    "search": [r"runSearch\(", r"switchView\(\s*\\?['\"]search"],
    "trending": [],
    "media-center": [],
}

# A screen with no browser test yet, and why it is acceptable for now. tests/test_every_feature_has_a_test.py fails
# when a listed screen GAINS a test (take it off) and when an unlisted one has none (write one, or list it with a
# reason). Shrink this list; never grow it to make a deploy pass.
KNOWN_GAPS = {
    "analytics": "My Analytics -- no browser test drives the charts yet",
    "articles": "long-form articles -- covered by unit tests of the card renderer only",
    "blackjack": "Nostr game -- no browser test plays it yet",
    "chess": "Nostr game -- no browser test plays it yet",
    "hangman": "Nostr game -- no browser test plays it yet",
    "holdem": "Nostr game -- no browser test plays it yet",
    "ttt": "Nostr game -- no browser test plays it yet",
    "texts": "Android SMS screen -- native; covered by the Android emulator checks in CI, not the gate",
    "xdc": "mini apps view -- the webxdc runtime is unit-tested; the screen itself is not driven yet",
}


def views() -> list:
    shell = (ROOT / "templates/client.html").read_text(errors="replace")
    found = set(re.findall(r'data-view="([a-z0-9_-]+)"', shell)) | set(EXTRA)
    return sorted(found - AI_VIEWS)


def _test_sources() -> dict:
    files = []
    for pat in ("tests/**/*.py", "tests/**/*.mjs", "tests/**/*.cjs", "scripts/check_*.py"):
        files += glob.glob(str(ROOT / pat), recursive=True)
    out = {}
    for f in files:
        rel = str(Path(f).relative_to(ROOT))
        if rel.endswith("check_every_view_opens.py") or rel.endswith("test_every_view_opens_in_the_gate.py"):
            continue            # the smoke check opens EVERYTHING; it is the floor, not feature coverage
        out[rel] = Path(f).read_text(errors="replace")
    return out


_BROWSER = re.compile(r"/opt/google/chrome|--headless|with_browser|_full_app|Page\.navigate|scripts/check_")


def coverage() -> dict:
    """{view: [browser test files that reach it]}"""
    src = _test_sources()
    browser = {f: s for f, s in src.items() if _BROWSER.search(f + "\n" + s)}
    # A parametrised test names the screens it drives with FEATURE_COVERS = ("ttt", "chess", ...): the view name is
    # then a parameter, not a literal switchView('ttt'), and an explicit declaration beats guessing from source.
    declared = {}
    for f, s in browser.items():
        for m in re.finditer(r"FEATURE_COVERS\s*=\s*[\(\[]([^\)\]]*)[\)\]]", s):
            for v in re.findall(r"['\"]([a-z0-9_-]+)['\"]", m.group(1)):
                declared.setdefault(v, set()).add(f)
    out = {}
    for v in views():
        q = re.escape(v)
        pats = [rf"""switchView\(\s*\\?['"]{q}\\?['"]""", rf"""switchMessagesTab\(\s*\\?['"]{q}\\?['"]""",
                rf"""data-view=\\?["']{q}\\?["']""", rf"#view-{q}\b"] + EXTRA.get(v, [])
        rx = re.compile("|".join(pats))
        out[v] = sorted({f for f, s in browser.items() if rx.search(s)} | declared.get(v, set()))
    return out


def main() -> int:
    cov = coverage()
    for v, tests in sorted(cov.items(), key=lambda kv: (len(kv[1]), kv[0])):
        flag = "GAP " if not tests else "    "
        why = f"  ({KNOWN_GAPS[v]})" if not tests and v in KNOWN_GAPS else ""
        print(f"{flag}{v:14} {len(tests):3} browser test(s){why}")
    gaps = [v for v, t in cov.items() if not t]
    print(f"\n{len(cov) - len(gaps)} of {len(cov)} screens have a browser test; {len(gaps)} gap(s) (AI screens excluded)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
