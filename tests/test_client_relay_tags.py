"""An event from a relay must never reach app code with `tags` missing.

Run: venv-unified/bin/python -m pytest tests/test_client_relay_tags.py

Companion to test_client_store_tags.py, which covers everything that reaches the CACHE. This covers
everything that does not. `tags` is REQUIRED by NIP-01, so nothing well-formed is rewritten — but a
relay is untrusted input, and the signature check cannot be relied on here: our OWN relay is
`trusted`, so its events skip verification entirely (`_onMessage`).

The callers that matter take a query result STRAIGHT from Relay without ever going through the
Store — the replaceable-list loaders in app.js (FOLLOWS, MUTED, PINNED, BOOKMARKS) all do
`ev.tags.filter(...)` on a raw result. A throw there does not cost one card the way the original
buildCounts bug did; it costs the follow list, on a codebase with a history of replaceable-list
wipes. Hence the guard sits at BOTH ingest points: the pool, and the one-shot external read that
bypasses the pool completely.

relay.js is a browser IIFE, so it is loaded here in a `vm` with stubbed browser globals and driven
through its real `_onMessage` — the shipped code, no reimplementation of the thing under test.
"""
import json
import os
import re
import shutil
import subprocess
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY = os.path.join(ROOT, "static", "js", "client", "relay.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(body):
    harness = textwrap.dedent(f"""
        const fs = require('fs'), vm = require('vm');
        const src = fs.readFileSync({json.dumps(RELAY)}, 'utf8');
        const ctx = {{ console, setTimeout, clearTimeout, setInterval, clearInterval,
                      WebSocket: function(){{}},
                      // relay.js starts its signing Worker at load; it is never exercised here.
                      Worker: function(){{ this.postMessage=()=>{{}}; this.terminate=()=>{{}};
                                          this.addEventListener=()=>{{}}; }},
                      document: {{ hidden:false, addEventListener(){{}} }},
                      localStorage: {{ _d:{{}}, getItem(k){{return this._d[k]||null}},
                                      setItem(k,v){{this._d[k]=String(v)}},
                                      removeItem(k){{delete this._d[k]}} }},
                      navigator: {{ onLine:true }}, crypto: require('crypto').webcrypto,
                      addEventListener(){{}},
                      location: {{ origin:'https://x', protocol:'https:', host:'x' }} }};
        ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
        vm.createContext(ctx);
        vm.runInContext(src, ctx);
        const Relay = ctx.window.Relay;

        // Deliver a raw relay frame through the REAL pooled ingest path and return what app code
        // would have been handed. `trusted` is the case that matters: it skips signature checks.
        function deliver(evJson, trusted){{
            let got = null;
            Relay._subs.set('s1', {{ onEvent: e => {{ got = e; }}, seen: new Set(),
                                    eosed: new Set(), filters: [{{}}], live: false }});
            Relay._onMessage({{ trusted: !!trusted, url: 'wss://t' }},
                             JSON.stringify(['EVENT', 's1', evJson]));
            Relay._subs.delete('s1');
            return got;
        }}
        {body}
    """)
    path = "/tmp/pcai-relay-tags-harness.js"
    with open(path, "w") as f:
        f.write(harness)
    out = subprocess.run(["node", path], capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr.decode()[:3000]
    return json.loads(out.stdout.decode())


def test_a_trusted_relay_cannot_deliver_an_event_with_no_tags():
    """Our own relay is `trusted`, so nothing verifies its events — this is the live exposure."""
    r = _run("""
        const ev = deliver({ id:'e1', pubkey:'a', kind:1, created_at:1, content:'x', sig:'s' }, true);
        // exactly what app.js's FOLLOWS/MUTED/PINNED/BOOKMARKS loaders do to a raw query result
        let err = null;
        try { ev.tags.filter(t => t[0] === 'p' && t[1]).map(t => t[1]); }
        catch (e) { err = String(e && e.message || e); }
        process.stdout.write(JSON.stringify({ tags: ev.tags, err }));
    """)
    assert r["err"] is None, f"a raw relay result still throws in the list loaders: {r['err']}"
    assert r["tags"] == []


def test_well_formed_events_are_delivered_untouched():
    """The guard must not rewrite real events — order and content are load-bearing downstream."""
    r = _run("""
        const tags = [['e','root','','root'], ['p','someone'], ['client','x']];
        const ev = deliver({ id:'e2', pubkey:'a', kind:1, created_at:1, tags, content:'x', sig:'s' }, true);
        process.stdout.write(JSON.stringify({ tags: ev.tags }));
    """)
    assert r["tags"] == [["e", "root", "", "root"], ["p", "someone"], ["client", "x"]]


def test_the_one_shot_external_read_is_guarded_too():
    """queryFrom opens its own socket and hands events straight back, bypassing the pool entirely —
    so the pooled guard does not cover it. Asserted on the source: it needs a live WebSocket."""
    src = open(RELAY, encoding="utf-8").read()
    m = re.search(r"if \(m\[0\] === 'EVENT' && m\[1\] === subId && m\[2\]\) got\.push\(([^)]*\))", src)
    assert m, "the one-shot external read no longer looks like this — re-check it normalises tags"
    assert "_normTags" in m.group(1), (
        f"queryFrom pushes relay events straight to callers without normalising: got.push({m.group(1)}")


def test_both_ingest_points_use_the_same_helper():
    """Two guards that can drift are worse than one — assert there is a single normaliser."""
    src = open(RELAY, encoding="utf-8").read()
    assert src.count("_normTags(") >= 3, "expected the definition plus both ingest call sites"
    assert re.search(r"_normTags\(ev\)\{[^}]*Array\.isArray\(ev\.tags\)", src), (
        "the normaliser no longer checks Array.isArray — a string or object `tags` would pass")
