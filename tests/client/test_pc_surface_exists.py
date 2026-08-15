"""Every `PC.<name>` a client module reaches for must actually be on `window.__PC`.

Run: venv-unified/bin/python -m pytest tests/client/test_pc_surface_exists.py

THE BUG THIS EXISTS FOR, and it reached a user. sync.js's "Empty trash" confirmation called
`PC._fmtBytes(...)` to say how much space was about to be freed. `_fmtBytes` is real, and it is NOT
on `PC` — it is one of ~40 names passed into git.js's factory, a list that reads exactly like an
export list unless you check what it is being passed to. So the call threw, the click handler's catch
turned it into `toast('failed: …')`, and the button reported **"action failed"** on an irreversible
action the user was trying to perform.

Everything about that is quiet. There is no build step and no type checker; `PC.anything` is valid
JavaScript that fails only when the line runs; and the lines most likely to be missed are exactly
these — error paths, confirmations and empty states, the ones nobody clicks while developing. A
module can look completely correct and be broken on the one screen that matters.

So this is a link check, run over the shipped files: collect what app.js actually publishes, collect
what every other module asks for, and require the second to be a subset of the first. It is cheap,
it is total, and it needs no browser.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "static" / "js" / "client"
APP = (CLIENT / "app.js").read_text(encoding="utf-8")

# Modules that receive `window.__PC` and call into it. app.js is excluded (it IS the surface) and so
# are the DOM-free engines, which are handed everything they need as arguments.
CONSUMERS = sorted(p for p in CLIENT.glob("*.js") if p.name not in {"app.js", "sw.js"})


def _strip(js: str) -> str:
    """Comments out. The note explaining THIS bug quotes `PC._fmtBytes`, and a checker that reads its
    own postmortem as a call site is a checker that cries wolf for ever."""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    js = re.sub(r"(?m)^\s*//.*$", "", js)
    js = re.sub(r"(?m)\s//[^\n'\"`]*$", "", js)
    return js


def _exported() -> set:
    """The keys of the `window.__PC = { … }` literal, brace-matched rather than guessed at."""
    i = APP.index("window.__PC = {")
    j, depth = i + len("window.__PC = "), 0
    while j < len(APP):
        if APP[j] == "{":
            depth += 1
        elif APP[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    body = _strip(APP[i + len("window.__PC = {"): j])
    names = set()
    # `foo,` / `foo: bar` / `async foo(` / `foo(a){` — every shape an object literal can name a key.
    for m in re.finditer(r"(?m)(?:^|[,{]|get\s|set\s|async\s)\s*([A-Za-z_$][\w$]*)\s*(?=[,:(}])", body):
        names.add(m.group(1))
    return names


def test_the_export_list_is_found_and_substantial():
    """Guard the guard: a brace-match that silently caught nothing would make every assertion below
    vacuous rather than false."""
    names = _exported()
    assert len(names) > 40, f"only found {len(names)} exports — the parse is wrong, not the code"
    for expected in ("toast", "uiConfirm", "syncBlobs", "mediaServer"):
        assert expected in names, f"{expected!r} missing — the parse is wrong"


def test_no_module_calls_something_PC_does_not_export():
    exported = _exported()
    missing = {}
    for path in CONSUMERS:
        src = _strip(path.read_text(encoding="utf-8"))
        for m in re.finditer(r"\bPC\.([A-Za-z_$][\w$]*)", src):
            name = m.group(1)
            if name in exported:
                continue
            # Optional-use guards are legitimate: `PC.foo && PC.foo(...)`, `PC.foo ? … : …`,
            # `typeof PC.foo === 'function'` — those are asking whether it exists, not assuming it.
            tail = src[m.end(): m.end() + 24]
            head = src[max(0, m.start() - 12): m.start()]
            if re.match(r"\s*(&&|\?|\)|===|!==|==|!=)", tail) or "typeof " in head:
                continue
            missing.setdefault(path.name, set()).add(name)
    assert not missing, (
        "these call into PC.<name> that window.__PC does not export — each one throws the first time "
        "that line runs, which for an error path or a confirmation is a button reporting "
        "'action failed': " + repr({k: sorted(v) for k, v in missing.items()}))


def test_the_check_can_fail():
    """The pre-fix line, run through the same rule."""
    exported = _exported()
    assert "_fmtBytes" not in exported, (
        "_fmtBytes is on PC now — this test's own example is stale, but more importantly the "
        "assertion above stopped being able to catch what it was written for")
    src = _strip("const s = PC._fmtBytes(stat.bytes);")
    found = [m.group(1) for m in re.finditer(r"\bPC\.([A-Za-z_$][\w$]*)", src)]
    assert found == ["_fmtBytes"] and found[0] not in exported


@pytest.mark.parametrize("name", ["toast", "uiConfirm"])
def test_the_ones_the_trash_button_depends_on(name):
    """Named explicitly because the Empty trash flow is now the most irreversible thing in this UI
    and it is reached through a confirmation — the exact place a missing helper is invisible until
    somebody presses it."""
    assert name in _exported()
