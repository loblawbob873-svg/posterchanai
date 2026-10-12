"""A write over one of the account's OWN private documents waits for an ANSWER -- and this node's relay
answering IS one, whatever any other relay in the pool does.

Run: venv-unified/bin/python -m pytest tests/client/test_private_doc_write_gates_hear_the_home_relay.py

Reported: a member with "use my own relays" (a 26-relay list) could never save his desktop layout.
`Relay.query()` marks a result `complete` only when EVERY relay in the pool sent an EOSE, and two of his
never do (filter.nostr.wine answers auth-required and the AUTH then waits 8s for an OK; rss.nos.social is
silent). The desktop was fixed in de1039bdf (`Relay.homeAnswered`); about a dozen other modules gate a
write of a kind-30078 `pcai:` document the same way -- client prefs, the vault key, the DM cache key,
the read markers, music-share answers, the VM host list, Concord's folders and read marks, ... -- and
each of them was blocked, held or "try again" for ever for that member. A few others had the OPPOSITE
bug: they took ANY resolved query as an answer (Budget, the go-live templates, the auto-mute switch,
the saved agent tasks and voices), so a read nobody answered armed the writer -- the replaceable-doc
wipe -- and the rule closes that too.

THE RULE, run from the shipped source under node (tests/client/home_relay_write_gates_runtime.mjs) in two
worlds: this node's relay answered while another relay never does -> the write goes out; nobody answered
-> nothing is written. `homeAnswered` is relay.js's own method, lifted from the file.

THE LIST IS DISCOVERED, never typed: every client module that publishes or signs a kind-30078 document
must be driven by the runner or carry, in its own source, `HOME-RELAY WRITE GATE: none -- <why>`. A new
module that writes such a document fails here until somebody decides which.

The browser half -- the real client against a real relay with a silent second relay, persist, then a
FRESH profile shows it -- is tests/client/test_private_docs_save_with_a_silent_relay_full_app.py.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "static" / "js" / "client"
RUNNER = Path(__file__).with_name("home_relay_write_gates_runtime.mjs")
MARKER = re.compile(r"HOME-RELAY WRITE GATE:\s*none\s*--\s*(.+)")
# Modules that are infrastructure, not features with documents of their own.
NOT_FEATURES = {"relay.js", "store.js", "sw.js"}

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _code(path):
    """The file with comment LINES dropped (line-based on purpose -- see
    tests/test_client_private_docs_are_classified.py for why a /* */ regex is unsafe here)."""
    out = []
    for ln in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        t = ln.lstrip()
        if t.startswith(("//", "*", "/*")):
            continue
        out.append(ln)
    return "\n".join(out)


def private_doc_writers():
    """Every client module that WRITES a kind-30078 document: a `publish(`/`sign(` of 30078 (or of a
    constant the module defines as 30078), or an event template with `kind: 30078`."""
    found = {}
    for path in sorted(CLIENT.glob("*.js")):
        if path.name in NOT_FEATURES:
            continue
        code = _code(path)
        consts = sorted(set(re.findall(r"\b([A-Z][A-Z0-9_]*)\s*=\s*30078\b", code)))
        kinds = "|".join(["30078"] + consts)
        hits = re.findall(r"(?:\b(?:publish|sign)\(\s*(?:%s)\b|\bkind\s*:\s*(?:%s)\b)" % (kinds, kinds), code)
        if hits:
            found[path.name] = len(hits)
    return found


@pytest.fixture(scope="module")
def gates():
    out = subprocess.run(["node", str(RUNNER)], capture_output=True, text=True, timeout=300, cwd=ROOT,
                         env={**os.environ, "PC_CLIENT_DIR": ""})
    assert out.returncode == 0, out.stderr[-3000:]
    return json.loads(out.stdout)


def test_the_discovery_is_not_vacuous():
    """The desktop layout is the document the report was about; a discovery that cannot see it can see
    nothing, and every other assertion here would pass on an empty set."""
    found = private_doc_writers()
    assert "os.js" in found and len(found) >= 10, found


def test_every_module_that_writes_a_private_doc_is_driven_or_says_why(gates):
    unexplained = []
    for name in private_doc_writers():
        if name in gates and any("home" in g for g in gates[name].values()):
            continue
        m = MARKER.search((CLIENT / name).read_text(encoding="utf-8", errors="ignore"))
        if m and len(m.group(1).strip()) >= 40:
            continue
        unexplained.append(name)
    assert not unexplained, (
        "these modules write a kind-30078 document, are not driven by home_relay_write_gates_runtime.mjs and do "
        "not say why in a `HOME-RELAY WRITE GATE: none -- <reason>` line: %s" % unexplained)


def test_a_marked_module_really_has_no_gate_to_widen(gates):
    """The marker is for modules whose writes are NOT decided by a read's completeness. One that is driven
    here has such a gate, so marking it too would hide it from the next reader."""
    both = [n for n in gates if MARKER.search((CLIENT / n).read_text(encoding="utf-8", errors="ignore"))]
    assert not both, both


def test_the_write_goes_out_once_this_nodes_relay_answered(gates):
    """Another relay in the pool never answering is what a member's own relay list looks like; it must
    not block, hold or refuse a write the node's relay -- which holds the document -- has answered for."""
    blocked = {f"{m} / {g}": r for m, gs in gates.items() for g, r in gs.items() if "home" in r and r["home"] is not True}
    assert not blocked, blocked


def test_nothing_is_written_when_nobody_answered(gates):
    """The replaceable-doc wipe: a read nobody answered is "could not ask", never "there is none"."""
    wrote = {f"{m} / {g}": r for m, gs in gates.items() for g, r in gs.items() if "nobody" in r and r["nobody"] is not False}
    assert not wrote, wrote


def test_dependency_injected_modules_are_handed_the_predicate(gates):
    """seen.js and livetemplates.js take `homeAnswered` as an option -- a caller that does not pass it
    leaves the module on `complete` alone, i.e. the bug, with every unit test of the module green."""
    wiring = {f"{m} / {g}": r for m, gs in gates.items() for g, r in gs.items() if "wired" in r}
    assert len(wiring) >= 2, wiring
    assert all(r["wired"] is True for r in wiring.values()), wiring
