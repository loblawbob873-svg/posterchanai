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


def state_shim(code: str) -> str:
    """`const S = {…};` for a node harness that runs a function lifted out of a split module.

    Inside a module, app.js's live `let`s are read as `S.ME`, `S.VIEW`, … (getters app.js hands the
    factory). A harness stubs those bindings under their own names (`let ME = …`), so this returns an
    `S` whose getters and setters ARE those stubs — declare it after them, in the same scope."""
    names = sorted(set(re.findall(r"(?<![\w$.])S\.([A-Za-z_$][\w$]*)", code)))
    return "const S={" + ",".join(
        f"get {n}(){{return typeof {n}!=='undefined'?{n}:undefined;}},set {n}(v){{{n}=v;}}"
        for n in names) + "};"


def client_files() -> list[tuple[str, str]]:
    """(name, text) for every split module and app.js, SEPARATELY — for a rule that is about one
    scope (each file is its own: app.js's IIFE, or one module's factory)."""
    return ([(n, module_path(n).read_text(encoding="utf-8")) for n in split_modules()]
            + [("app.js", APP.read_text(encoding="utf-8"))])


def client_source_path_for(needle: str) -> Path:
    """The ONE shipped file that contains `needle` (app.js or a split module) — for tests that must
    hand a real path to node or git. Raises if it is in none or in more than one."""
    hits = [p for p in [APP] + [module_path(n) for n in split_modules()]
            if needle in p.read_text(encoding="utf-8")]
    if len(hits) != 1:
        raise AssertionError(f"{needle!r} is in {len(hits)} client files: {[h.name for h in hits]}")
    return hits[0]
