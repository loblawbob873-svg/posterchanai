"""A realtime packet must reach a relay that is UP, not only the first one in the list.

Run: venv-unified/bin/python -m pytest tests/test_client_relay_publish_fast.py

`publishFast` is the transport for webxdc's realtime channel (ephemeral kind 20932) — a moving
player sends 20-30 packets a second, so it deliberately targets ONE relay rather than fanning out
across somebody's whole pool. It picked `this.url`, which is simply `urls[0]`, and fell back to "any
connection" only when the map had no entry at all.

That fallback can never fire. A relay that is down or reconnecting KEEPS its entry in `_conns` for
the whole session — that is what makes it come back — so the lookup succeeded, found a socket in
CONNECTING or CLOSED, and returned false. Every packet was dropped, silently, for as long as the
first relay was reconnecting, with the rest of the pool connected and idle. And two players whose
relay lists simply START with different hosts could never see each other's movement at all, which
reads as "multiplayer doesn't work" rather than as one relay being down.

Driven through the real relay.js in a `vm` against scripted sockets, like the EOSE-gate test beside
it: nothing static can see that a fallback is unreachable.
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

DEAD = "wss://never-answers/"
LIVE_A = "wss://alive-one/"
LIVE_B = "wss://alive-two/"


def _run(body):
    src = open(RELAY, encoding="utf-8").read()
    harness = textwrap.dedent(
        """
        const vm = require('vm');
        const src = %(src)s;
        const DEAD = %(dead)s;
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
        FakeWS.prototype.send = function(s){ this.sent.push(s); };
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
        // Where did the packet actually land? One entry per url that saw this EVENT.
        const landed = (id) => FakeWS.opened.filter(
          w => w.sent.some(s => s.indexOf('"' + id + '"') >= 0)).map(w => w.url);
        (async () => {
        %(body)s
        })().catch(e => { console.error(e && e.stack || e); process.exit(1); });
        """
        % {"src": json.dumps(src), "dead": json.dumps(DEAD),
           "body": textwrap.indent(textwrap.dedent(body), "        ")}
    )
    path = "/tmp/pcai-relay-publishfast-harness.js"
    with open(path, "w") as f:
        f.write(harness)
    proc = subprocess.run(["node", path], capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode()[:3000]
    return json.loads(proc.stdout.decode())


PACKET = "{ id:'pkt1', kind:20932, content:'x', tags:[], pubkey:'p', sig:'s', created_at:1 }"


def test_a_reconnecting_first_relay_does_not_swallow_every_packet():
    r = _run("""
        Relay.configure({ urls: [%(dead)s, %(a)s], verify: false });
        await sleep(120);
        const ok = Relay.publishFast(%(pkt)s);
        out({ ok, landed: landed('pkt1'), conns: Relay.urls().length });
    """ % {"dead": json.dumps(DEAD), "a": json.dumps(LIVE_A), "pkt": PACKET})
    assert r["conns"] == 2, "the dead relay should still be in the pool — it is retrying"
    assert r["ok"] is True, "the packet was dropped while a live socket was sitting there"
    assert r["landed"] == [LIVE_A]


def test_it_still_prefers_the_pools_primary_when_that_one_is_up():
    """One relay per packet is the whole design — 30 packets a second across somebody's entire pool
    is a flood aimed at strangers' infrastructure. The peers are on this instance's relay, which is
    `urls[0]`, so that is the one to use whenever it is available."""
    r = _run("""
        Relay.configure({ urls: [%(a)s, %(b)s], verify: false });
        await sleep(120);
        const ok = Relay.publishFast(%(pkt)s);
        out({ ok, landed: landed('pkt1') });
    """ % {"a": json.dumps(LIVE_A), "b": json.dumps(LIVE_B), "pkt": PACKET})
    assert r["ok"] is True
    assert r["landed"] == [LIVE_A], "a realtime packet must not be fanned out across the pool"


def test_a_named_relay_that_is_down_falls_through_rather_than_dropping():
    r = _run("""
        Relay.configure({ urls: [%(a)s, %(dead)s], verify: false });
        await sleep(120);
        const ok = Relay.publishFast(%(pkt)s, %(dead)s);
        out({ ok, landed: landed('pkt1') });
    """ % {"a": json.dumps(LIVE_A), "dead": json.dumps(DEAD), "pkt": PACKET})
    assert r["ok"] is True
    assert r["landed"] == [LIVE_A]


def test_with_nothing_connected_it_reports_the_failure():
    """The caller drops the packet on false (a stale move is worth nothing). That answer has to stay
    reachable, or a queue builds behind a pool that is entirely down."""
    r = _run("""
        Relay.configure({ urls: [%(dead)s], verify: false });
        await sleep(120);
        out({ ok: Relay.publishFast(%(pkt)s), landed: landed('pkt1') });
    """ % {"dead": json.dumps(DEAD), "pkt": PACKET})
    assert r["ok"] is False
    assert r["landed"] == []
