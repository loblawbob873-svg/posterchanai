"""The local relay pins EXACTLY what the client's cache pins.

PosterChanOS's local relay replaces the client's IndexedDB cache, and its LRU cap must never evict what the
client's own cache never evicts: `_isPinned` in static/js/client/store.js (notes, the vault, the drive index,
the SMS archive, the desktop layout…). The relay's copy of that rule is app/services/nostr_relay/os_pins.py.
Every private document in this app has at some point been missing from one of two lists that had to agree
(CLAUDE.md: `_isPinned` and `_CARRY_D`), so this reads the d-tag rule OUT OF store.js -- every
`t[1].startsWith('…')` and `t[1] === '…'` inside `_isPinned` -- and fails the day a document is pinned in
one place and not the other. It also RUNS the shipped `_isPinned` under node (through Store's eviction) on a
set of events and asserts the Python predicate agrees with it event for event.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.nostr_relay import os_pins

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "static" / "js" / "client" / "store.js"


def _is_pinned_source() -> str:
    src = STORE.read_text()
    start = src.index("function _isPinned(ev){")
    end = src.index("\n  }\n", start)
    return src[start:end]


def test_the_d_tag_lists_are_the_clients():
    body = _is_pinned_source()
    prefixes = set(re.findall(r"t\[1\]\.startsWith\('([^']+)'\)", body))
    exact = set(re.findall(r"t\[1\] === '(pcai:[^']+)'", body))
    assert prefixes and exact, "could not read the rule out of store.js -- the test is not measuring anything"
    assert prefixes == set(os_pins.D_PREFIXES) | {os_pins.D_OWN_PREFIX}, \
        ("pinned d-tag PREFIXES differ between store.js and os_pins.py", prefixes ^ (set(os_pins.D_PREFIXES) | {os_pins.D_OWN_PREFIX}))
    assert exact == set(os_pins.D_EXACT), ("pinned d-tags differ between store.js and os_pins.py", exact ^ set(os_pins.D_EXACT))
    src = STORE.read_text()
    for name, value in (("XDC_PIN_MAX", os_pins.XDC_PIN_MAX), ("NOTIF_PIN_MAX", os_pins.NOTIF_PIN_MAX)):
        assert re.search(r"const %s = %d;" % (name, value), src), (name, "cap differs from the client's")
    kinds = set(int(k) for k in re.search(r"NOTIF_KINDS = new Set\(\[([^\]]+)\]\)", src).group(1).split(","))
    assert kinds == set(os_pins.NOTIF_KINDS)


def _cases(me, them):
    out = []
    n = 0
    for kind in (1, 7, 30078, 1063, 6, 30023):
        for d in ["pcai:note:1", "pcai:notefolder:2", "pcai:pw:item", "pcai:sms:1", "pcai:files-index",
                  "pcai:playlist:x", "pcai:budget", "pcai:budgetx", "pcai:desktop", "pcai:seen", "pcai:dmkey",
                  "pcai:vmhosts", "pcai:fs:pair:abc", "pcai:kv:x", "pcai:musicshare:abc", "other", None]:
            for author in (me, them):
                for extra in ([], [["p", me]], [["m", "application/x-webxdc"]]):
                    tags = ([["d", d]] if d else []) + extra
                    n += 1
                    out.append({"id": "%064x" % n, "pubkey": author, "kind": kind, "created_at": 1000 + n,
                                "tags": tags, "content": "", "sig": "x"})
    return out


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_the_python_predicate_agrees_with_the_shipped_one_event_for_event(tmp_path):
    me, them = "a" * 64, "b" * 64
    cases = _cases(me, them)
    harness = tmp_path / "h.js"
    harness.write_text("""
const fs=require('fs'),vm=require('vm');
const src=fs.readFileSync(%s,'utf8');
const ctx={console,setTimeout,clearTimeout,setInterval,clearInterval,indexedDB:undefined,
  localStorage:{_d:{},getItem(k){return this._d[k]||null},setItem(k,v){this._d[k]=String(v)},removeItem(k){delete this._d[k]}},
  navigator:{onLine:true}};
ctx.window=ctx;ctx.self=ctx;ctx.globalThis=ctx;vm.createContext(ctx);
// expose the private predicate the same way the file is evaluated, nothing else changed
vm.runInContext(src.replace('Store.setViewer = setViewer;','Store.setViewer = setViewer; Store.__isPinned = _isPinned;'),ctx);
const S=ctx.window.Store; S.setViewer(%s);
const cases=%s;
process.stdout.write(JSON.stringify(cases.map(e=>!!S.__isPinned(e))));
""" % (json.dumps(str(STORE)), json.dumps(me), json.dumps(cases)))
    out = subprocess.run(["node", str(harness)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    js = json.loads(out.stdout)
    py = [os_pins.is_pinned(e, me) for e in cases]
    assert any(js) and not all(js), "the case set does not exercise the rule"
    diff = [(c["kind"], c["tags"], c["pubkey"][:1], j, p) for c, j, p in zip(cases, js, py) if j != p]
    assert not diff, ("the relay and the client disagree about what is pinned", diff[:10])
