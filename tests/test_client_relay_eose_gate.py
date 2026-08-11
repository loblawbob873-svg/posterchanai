"""One unreachable relay in the pool must not slow down — or empty — the whole app.

Run: venv-unified/bin/python -m pytest tests/test_client_relay_eose_gate.py

`Relay.query()` is the one-shot fetch behind every deferred fill in the client: repost originals,
notification previews, quoted notes, thread parents, older timeline pages. It resolved only once
`sub.eosed.size >= this._conns.size` — and that denominator counted relays that had never been
asked anything. A relay that is DOWN stays in `_conns` for the whole session (it is in a reconnect
backoff, which is what makes it come back), and a REQ is silently dropped for any socket that is not
OPEN (`Conn._send`). So one dead relay made the threshold unreachable and every query in the app ran
to its full 6s timeout, forever.

That is not a latency story, it is a data story: `loadOlderTimeline` read the resulting empty result
as "the relay has nothing older" and latched `_tl.done`, so Home/Global/Trending stopped loading for
the session with no spinner and no error. Reproduced in the field with `wss://offchain.pub/` — one of
the six relays this client itself suggests — unreachable.

relay.js is a browser IIFE, so it runs here in a `vm` against a scripted WebSocket and is driven
through its real configure/subscribe/query path. The assertion is on the CLOCK: a query that resolves
in tens of milliseconds waited for the relays that answered; one that takes ~6s waited for the corpse.
"""
import json
import os
import shutil
import subprocess
import textwrap

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAY = os.path.join(ROOT, "static", "js", "client", "relay.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

LIVE_A = "wss://alive-one/"
LIVE_B = "wss://alive-two/"
DEAD = "wss://never-answers/"

# The query timeout. A test that trips the bug pays this in wall clock; a passing one pays ~nothing.
TIMEOUT_MS = 6000


def _run(body, src_override=None):
    src = src_override if src_override is not None else open(RELAY, encoding="utf-8").read()
    harness = textwrap.dedent(
        """
        const vm = require('vm');
        const src = %(src)s;
        const DEAD = %(dead)s;
        // A scripted relay socket. Opens after a tick and EOSEs any REQ it is sent — except the dead
        // one, which errors and closes exactly as an unreachable host does, and is then reopened by
        // relay.js's own backoff for as long as the test runs (that persistence IS the bug's fuel).
        function FakeWS(url){
          this.url = url; this.readyState = 0; this.sent = [];
          FakeWS.opened.push(this);
          const dead = (url === DEAD);
          setTimeout(() => {
            if (dead){ this.readyState = 3; this.onerror && this.onerror({}); this.onclose && this.onclose({}); }
            else { this.readyState = 1; this.onopen && this.onopen(); }
          }, 5);
        }
        FakeWS.opened = [];
        FakeWS.prototype.send = function(s){
          this.sent.push(s);
          let m; try { m = JSON.parse(s); } catch(_){ return; }
          if (m[0] !== 'REQ') return;
          if (this._stall) return;                     // "up, but never answers this filter"
          setTimeout(() => { this.onmessage && this.onmessage({ data: JSON.stringify(['EOSE', m[1]]) }); }, 5);
        };
        FakeWS.prototype.close = function(){ this.readyState = 3; };

        const ctx = { console, setTimeout, clearTimeout, setInterval, clearInterval, process,
                      WebSocket: FakeWS, FakeWS,
                      Worker: function(){ this.postMessage=()=>{}; this.terminate=()=>{};
                                          this.addEventListener=()=>{}; },
                      document: { hidden:false, addEventListener(){} },
                      localStorage: { _d:{}, getItem(k){return this._d[k]||null},
                                      setItem(k,v){this._d[k]=String(v)}, removeItem(k){delete this._d[k]} },
                      navigator: { onLine:true }, crypto: require('crypto').webcrypto,
                      addEventListener(){},
                      location: { origin:'https://x', protocol:'https:', host:'x' } };
        ctx.window = ctx; ctx.self = ctx; ctx.globalThis = ctx;
        vm.createContext(ctx);
        vm.runInContext(src, ctx);
        const Relay = ctx.window.Relay;
        const out = (o) => { process.stdout.write(JSON.stringify(o)); process.exit(0); };
        const sleep = (ms) => new Promise(r => setTimeout(r, ms));
        (async () => {
        %(body)s
        })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """
        % {
            "src": json.dumps(src),
            "dead": json.dumps(DEAD),
            "body": textwrap.indent(textwrap.dedent(body), "        "),
        }
    )
    path = "/tmp/pcai-relay-eose-harness.js"
    with open(path, "w") as f:
        f.write(harness)
    proc = subprocess.run(["node", path], capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode()[:3000]
    return json.loads(proc.stdout.decode())


# The relay.js that HAD the bug, addressed by BLOB, not by a revision.
#
# These two tests read as "HEAD" for about ten minutes: the moment the fix is committed, HEAD contains
# it and the tests that exist to prove the bug was real start failing — a test that invalidates itself
# on the commit it ships in. A blob hash is the file, permanently, and cannot drift with the branch.
_BUGGY_RELAY_BLOB = "90802a1aea80c643b368b554b8152094d092234a"   # static/js/client/relay.js at 5d2a19ac


def _old_relay_src():
    p = subprocess.run(["git", "cat-file", "-p", _BUGGY_RELAY_BLOB], cwd=ROOT, capture_output=True)
    if p.returncode != 0:
        pytest.skip("the pre-fix relay.js blob is not in this clone")
    return p.stdout.decode()


BODY_DEAD_RELAY = """
    Relay.configure({ urls: [%(a)s, %(b)s, %(dead)s], verify: false });
    await sleep(120);                       // sockets settle: two open, one dead and retrying
    const t0 = Date.now();
    const evs = await Relay.query([{ ids: ['deadbeef'] }], %(t)s);
    out({ ms: Date.now() - t0, complete: evs.complete !== false, conns: Relay.urls().length });
""" % {"a": json.dumps(LIVE_A), "b": json.dumps(LIVE_B), "dead": json.dumps(DEAD), "t": TIMEOUT_MS}


def test_a_dead_relay_does_not_hold_every_query_to_the_timeout():
    r = _run(BODY_DEAD_RELAY)
    assert r["conns"] == 3, "the dead relay should still be in the pool — it is retrying, that is the point"
    assert r["ms"] < 1000, (
        f"query took {r['ms']}ms with one unreachable relay in the pool — it is still waiting on a "
        "socket that was never sent the REQ"
    )
    assert r["complete"] is True, "every relay we actually asked answered, so the result IS complete"


def test_that_bug_is_what_the_old_code_did():
    """Same scenario against the previous relay.js — proves the assertion above can fail."""
    r = _run(BODY_DEAD_RELAY, src_override=_old_relay_src())
    assert r["ms"] >= TIMEOUT_MS - 200, (
        f"expected the old gate to burn the full {TIMEOUT_MS}ms timeout, got {r['ms']}ms"
    )
    assert r["complete"] is False


BODY_COLD_START = """
    Relay.configure({ urls: [%(a)s, %(b)s], verify: false });
    // No sleep: this is the cold start — the first queries fire while the sockets are still
    // CONNECTING, so Conn._send drops their REQ on the floor and nothing ever answers them.
    const t0 = Date.now();
    const evs = await Relay.query([{ ids: ['deadbeef'] }], %(t)s);
    out({ ms: Date.now() - t0, complete: evs.complete !== false });
""" % {"a": json.dumps(LIVE_A), "b": json.dumps(LIVE_B), "t": TIMEOUT_MS}


def test_a_query_fired_before_the_sockets_open_still_gets_asked():
    r = _run(BODY_COLD_START)
    assert r["ms"] < 1000, (
        f"a query issued during connect took {r['ms']}ms — its REQ was dropped and never re-sent"
    )
    assert r["complete"] is True


def test_the_cold_start_used_to_time_out():
    r = _run(BODY_COLD_START, src_override=_old_relay_src())
    assert r["ms"] >= TIMEOUT_MS - 200, f"expected the old code to time out, got {r['ms']}ms"


def test_a_relay_that_drops_mid_query_leaves_the_denominator():
    """A socket that dies after taking the REQ cannot answer it either — it must stop being waited on."""
    r = _run("""
        Relay.configure({ urls: [%(a)s, %(b)s], verify: false });
        await sleep(120);
        // Make BOTH stall so nothing EOSEs on its own, then kill one: the survivor's EOSE has to be
        // enough. (If the drop did not clear it, this can only end at the timeout.)
        for (const ws of FakeWS.opened) ws._stall = true;
        const t0 = Date.now();
        const p = Relay.query([{ ids: ['deadbeef'] }], %(t)s);
        await sleep(30);
        const live = FakeWS.opened.filter(w => w.readyState === 1);
        live[0].readyState = 3; live[0].onclose && live[0].onclose({});      // one relay drops
        live[1]._stall = false;
        const req = JSON.parse(live[1].sent.filter(s => s.startsWith('["REQ"'))[0]);
        live[1].onmessage({ data: JSON.stringify(['EOSE', req[1]]) });       // the survivor answers
        const evs = await p;
        out({ ms: Date.now() - t0, complete: evs.complete !== false });
    """ % {"a": json.dumps(LIVE_A), "b": json.dumps(LIVE_B), "t": TIMEOUT_MS})
    assert r["ms"] < 1000, f"a dropped socket is still being waited on ({r['ms']}ms)"


def test_a_live_relay_that_never_answers_still_holds_the_gate():
    """The fix must not turn into 'resolve on the first EOSE' — a relay that is UP and simply slow to
    answer this filter is still owed the wait, or a query returns half an answer as complete."""
    r = _run("""
        Relay.configure({ urls: [%(a)s, %(b)s], verify: false });
        await sleep(120);
        FakeWS.opened.filter(w => w.readyState === 1 && w.url === %(b)s)[0]._stall = true;
        const t0 = Date.now();
        const evs = await Relay.query([{ ids: ['deadbeef'] }], 800);
        out({ ms: Date.now() - t0, complete: evs.complete !== false });
    """ % {"a": json.dumps(LIVE_A), "b": json.dumps(LIVE_B)})
    assert r["ms"] >= 700, f"resolved in {r['ms']}ms — it stopped waiting for a relay that is up"
    assert r["complete"] is False, "it gave up on the timer, so the result is partial and must say so"
