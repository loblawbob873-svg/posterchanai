"""AN OP NO TEST DRIVES IS AN OP THAT DOES NOT WORK, AND YOU FIND OUT FROM A USER.

The Concord bridge exposes six operations. Its end-to-end test drove four of them — it started
with a bundle already in hand and went straight to `inspect`, so `inviteDetails` and `openInvite`,
the two a bot uses to JOIN A ROOM, were never executed by anything. Both were broken: the vm realm
had no `URL`, cord-protocol's invite parser is `try { new URL(t) } catch { return }`, and so every
real invite link came back "invalid CORD invite" while the bare `naddr1…#secret` form worked
perfectly. A bot could not have joined a room from a pasted link at all, and the suite was green.

Nothing about that was subtle in hindsight. What was missing was the question "is every door
opened?" — so this asks it mechanically, and it will ask it again for the next op somebody adds.

IT ALSO PINS THE REALM TO ONE DEFINITION. The bridge had a realm and the test fixture built its
own, and a fixture carrying the same gap as the code cannot see the gap: it reproduces the bug and
calls the result expected. That is not a Concord-specific hazard — it is the most common way a test
suite here has been wrong (the Concord scroll fixture delivered a scroll event to one of the two
listeners a browser reaches; the osshell fixture defined `globalThis.PC` when the client publishes
`window.__PC`, so it agreed with a bug that made every toast vanish). One realm, imported by both.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = (ROOT / "botframework/concord_bridge.mjs").read_text(encoding="utf-8")
REALM = ROOT / "botframework/cord_realm.mjs"


def _ops() -> set[str]:
    """The op table, read from the shipped bridge rather than typed here."""
    block = BRIDGE[BRIDGE.index("const ops = {"):]
    block = block[:block.index("\n};")]
    # Each op is `name: (…) =>` or `name: async (…) =>`, at one indent level.
    return set(re.findall(r"^  ([a-zA-Z][a-zA-Z0-9]*):\s*(?:async\s*)?\(", block, re.M))


def _test_text() -> str:
    """Everything the suite says, so an op driven from ANY test counts."""
    out = []
    for path in sorted(ROOT.glob("tests/**/*.py")) + sorted(ROOT.glob("tests/**/*.mjs")):
        if path.name == Path(__file__).name:
            continue                      # this file names every op; it must not vouch for itself
        try:
            out.append(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return "\n".join(out)


def test_the_op_table_is_readable():
    """The check before the check: a parse that finds nothing makes the rule below pass about an
    empty set, which is how a guard stops guarding without anybody noticing."""
    ops = _ops()
    assert len(ops) >= 5, f"the bridge's op table no longer parses: {ops}"
    assert {"inviteDetails", "openInvite", "inspect", "read", "say"} <= ops, ops


def _wrappers() -> dict[str, set[str]]:
    """op → the Python Room methods that call it.

    Most ops are reached through `concord.Room`, not named directly, so a test that joins a room is
    exercising `inviteDetails` without ever writing the word. Following the wrapper is what keeps
    this rule honest in both directions: it does not demand the op name be typed in a test, and it
    does not accept the wrapper merely EXISTING as evidence that anything ran.
    """
    src = (ROOT / "botframework/concord.py").read_text(encoding="utf-8")
    out: dict[str, set[str]] = {}
    method = None
    for line in src.splitlines():
        m = re.match(r"\s*def ([a-z_]+)\(", line)
        if m:
            method = m.group(1)
        for op in re.findall(r'bridge\.call\("([a-zA-Z]+)"', line):
            out.setdefault(op, set())
            if method:
                out[op].add(method)
    return out


def test_every_op_is_actually_driven_by_a_test():
    text, wrappers = _test_text(), _wrappers()
    untested = []
    for op in sorted(_ops()):
        names = {op} | wrappers.get(op, set())
        if not any(f"'{n}'" in text or f'"{n}"' in text or f".{n}(" in text for n in names):
            untested.append(op)
    assert not untested, (
        "these bridge ops are exposed to bots and no test executes them, directly or through their "
        "Room wrapper: %s. That is exactly how inviteDetails and openInvite shipped broken — a bot "
        "could not join a room from a pasted link, and the suite was green."
        % ", ".join(untested))


def test_the_realm_is_defined_once():
    """A second hand-built realm is a second set of gaps, and the copy that lives in the TEST is the
    dangerous one: it makes the fixture agree with the bug."""
    assert REALM.exists(), "the shared CORD realm is gone"
    assert "makeRealm" in BRIDGE and "cord_realm.mjs" in BRIDGE, (
        "the bridge no longer imports the shared realm — if it builds its own again, a fixture "
        "cannot detect what it is missing")
    fixture = (ROOT / "tests/mint_concord_room.mjs").read_text(encoding="utf-8")
    assert "cord_realm.mjs" in fixture, (
        "the Concord test fixture builds its own realm again; it will reproduce whatever the "
        "bridge's realm is missing and report it as expected behaviour")
    assert "vm.createContext" not in fixture, fixture[:200]


def test_the_realm_supplies_what_the_cord_modules_actually_reach_for():
    """Each of these was missing once, and each failure was misleading rather than loud.

    `URL` is the one that matters most here: its absence is SWALLOWED by the parser's own catch, so
    it produced a wrong answer ("invalid CORD invite") rather than an error.
    """
    realm = REALM.read_text(encoding="utf-8")
    for needed in ("TextEncoder", "TextDecoder", "crypto", "btoa", "atob",
                   "performance", "URL", "document", "location"):
        assert f"globalThis.{needed}" in realm, (
            f"the CORD realm no longer defines {needed}; the modules reach for it and a vm context "
            "has none")


def test_nothing_from_the_host_realm_is_handed_across():
    """THE RULE THAT HAS BEEN BROKEN FOUR TIMES. A value built in the host realm fails every
    `instanceof` and prototype check on the other side — reported as "expected Uint8Array, got
    object" and "can't serialize event with wrong or missing properties", neither of which points
    anywhere near the cause. Everything crosses as a plain Array/string/object and is rebuilt."""
    realm = REALM.read_text(encoding="utf-8")
    ctx = realm[realm.index("vm.createContext({"):realm.index("vm.runInContext(")]
    # The host helpers may only return JSON-shaped values. A bare `new URL(...)`/`new Uint8Array`
    # handed back would be a host object.
    for line in ctx.splitlines():
        if "return" in line and "new " in line and "__" not in line.split("return")[0]:
            assert "Array.from" in line or "{" in line.split("return")[1], (
                "a host object looks like it crosses the realm boundary: " + line.strip())
    assert "Array.from(new TextEncoder()" in realm, (
        "the encoder shim no longer flattens to a plain Array on the way across")
