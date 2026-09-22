"""The web client's source as a test should read it: the modules split out of app.js, plus app.js.

app.js was one 2.9 MB IIFE. Screens that are not needed for the first paint now live in their own
files (mail.js, …) as factories that app.js builds on first use — see `_lzGet` in app.js. The moved
code is byte-identical apart from its live-state reads (`S.ME` for `ME`, and so on), so a test that
asserts on a function's TEXT keeps asserting on exactly the same text; it only has to look in the
right file. Reading this instead of app.js alone makes that true for every test at once, and stays
true as more screens move, because the list of split modules is read from app.js itself rather
than typed here.

Nothing is rewritten or normalised: a test that reads `client_source()` sees the shipped bytes of
every file, joined with a newline.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "static" / "js" / "client"
APP = CLIENT / "app.js"


def split_modules() -> list[str]:
    """File names of the modules app.js loads through its lazy-module loader, in first-use order."""
    app = APP.read_text(encoding="utf-8")
    seen: list[str] = []
    for name in re.findall(r"_lzGet\('([\w.-]+\.js)'", app):
        if name not in seen:
            seen.append(name)
    return seen


def module_path(name: str) -> Path:
    return CLIENT / name


def client_source() -> str:
    """Every split module, then app.js, as shipped.

    The modules come FIRST on purpose. app.js keeps a one-line entry point under the same name as
    each function that moved (`function renderMailView(){ return _lzRun(…) }`), so a test that finds
    a function by `src.index("function renderMailView(")` must meet the real definition before the
    entry point, exactly as it did when the real one was the only one."""
    parts = [module_path(name).read_text(encoding="utf-8") for name in split_modules()]
    parts.append(APP.read_text(encoding="utf-8"))
    return "\n".join(parts)


def client_source_path_for(needle: str) -> Path:
    """The ONE shipped file that contains `needle` (app.js or a split module) — for tests that must
    hand a real path to node or git. Raises if it is in none or in more than one."""
    hits = [p for p in [APP] + [module_path(n) for n in split_modules()]
            if needle in p.read_text(encoding="utf-8")]
    if len(hits) != 1:
        raise AssertionError(f"{needle!r} is in {len(hits)} client files: {[h.name for h in hits]}")
    return hits[0]
