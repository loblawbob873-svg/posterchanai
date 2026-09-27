"""A lost OK is recovered on a pool of the user's OWN relays, not only on trusted ones.

Reported as a git issue (d2376b43): 'At publish some issue on git get the message "timeout" but the
issue was published already' -- and the same person's previous report sat on the repo FOUR times in
51 seconds. Measured: every copy reached this node's relay DIRECTLY and was stored, and the relay
answers OK in 2-4 ms, so the phone never received the OKs (a connection that could send but no longer
receive). relay.js has a recovery for exactly that -- a relay's echo of the event, a read-back by id,
rebuilding a silent socket -- but all three were gated on `conn.trusted`, and with "use my own relays"
on the pool is verify:true, where NOTHING is trusted. So every lost OK became an 8-second "timeout",
the issue form re-enabled Publish, and each press signed a new duplicate.

Drives the SHIPPED relay.js under node, verify:true, against stub sockets.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RELAY = ROOT / "static" / "js" / "client" / "relay.js"

DRIVER = r"""
class FakeWS {
  constructor(url){ this.url = url; this.readyState = 0; this.sent = []; FakeWS.all.push(this);
    setTimeout(()=>{ this.readyState = 1; this.onopen && this.onopen(); }, 0); }
  send(s){ this.sent.push(JSON.parse(s)); }
  close(){ this.readyState = 3; }
  reply(arr){ this.onmessage && this.onmessage({ data: JSON.stringify(arr) }); }
}
FakeWS.all = [];
global.WebSocket = FakeWS; global.window = global; global.self = global;
global.Worker = class { constructor(){} postMessage(){} addEventListener(){} terminate(){} };
global.document = { addEventListener(){}, hidden: false, visibilityState: 'visible' };
global.location = { origin: 'https://x.test', protocol: 'https:' };
global.navigator = { onLine: true };
global.indexedDB = { open(){ const r = {}; setTimeout(()=>{ r.onerror && r.onerror(); },0); return r; } };
require(process.argv[2]);
const Relay = global.Relay, sleep = ms => new Promise(r => setTimeout(r, ms));
let n = 0;
const ev = () => { n++; return { id: String(n).padStart(64, 'a'), kind: 1621, content: 'issue', sig: String(n).padStart(128, 'f'),
                                 pubkey: 'b'.repeat(64), created_at: 1, tags: [] }; };
(async () => {
  const out = {};
  // The user's own relays (nothing trusted); this instance's relay is among them as `home`.
  Relay.configure({ urls: ['wss://mine.test'], verify: true, home: 'wss://mine.test' });
  await sleep(30);
  const first = FakeWS.all[0];

  // 1. The relay echoes our event on a live subscription; its OK frame was lost.
  { const e = ev(); const p = Relay.publish(e, 4000); await sleep(10);
    first.reply(['EVENT', 'live-sub', e]); const r = await p; out.echo = [r.ok, r.msg]; }

  // 2. No echo; the read-back by id finds it.
  { const e = ev(); const t0 = Date.now(); const p = Relay.publish(e, 4000); await sleep(900);
    const req = [...first.sent].reverse().find(x => x[0] === 'REQ' && x[2] && x[2].ids && x[2].ids[0] === e.id);
    out.readbackAsked = !!req;
    if (req) first.reply(['EVENT', req[1], e]);
    const r = await p; out.readback = [r.ok, Date.now() - t0 < 3000]; }

  // 3. The same id with somebody else's signature is NOT our event and proves nothing.
  { const e = ev(); const p = Relay.publish(e, 1500); await sleep(10);
    first.reply(['EVENT', 'live-sub', { ...e, sig: 'e'.repeat(128) }]); const r = await p; out.forged = r.ok; }

  // 4. A socket that has gone completely silent is rebuilt, the event is re-sent on the new one,
  //    and that relay's OK settles the publish.
  { const before = FakeWS.all.length, e = ev(); const p = Relay.publish(e, 6000);
    await sleep(2600);                                  // the read-back times out on a silent socket
    const fresh = FakeWS.all.slice(before).find(w => w.url.includes('mine.test'));
    out.rebuilt = !!fresh;
    if (fresh) { await sleep(50); out.resent = fresh.sent.some(x => x[0] === 'EVENT' && x[1].id === e.id);
                 fresh.reply(['OK', e.id, true, '']); }
    const r = await p; out.silent = r.ok; }

  console.log(JSON.stringify(out)); process.exit(0);
})();
"""


@pytest.fixture(scope="module")
def result():
    if not shutil.which("node"):
        pytest.skip("node not installed")
    with tempfile.TemporaryDirectory() as d:
        drv = Path(d) / "drv.js"
        drv.write_text(DRIVER)
        r = subprocess.run(["node", str(drv), str(RELAY)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr[-2000:]
        return json.loads(r.stdout.strip().splitlines()[-1])


def test_an_echo_of_our_own_event_is_delivery_on_the_users_own_relays(result):
    assert result["echo"][0] is True, f"the relay echoed the event and the publish still failed: {result['echo']}"


def test_the_read_back_confirms_it_well_before_the_timeout(result):
    assert result["readbackAsked"], "the read-back by id was never sent"
    assert result["readback"] == [True, True], result["readback"]


def test_a_copy_with_another_signature_is_not_our_event(result):
    assert result["forged"] is False, "a same-id event with a different signature was taken as delivery"


def test_a_silent_socket_is_rebuilt_and_the_event_resent(result):
    """Only for this instance's own relay: a stranger's relay is never churned (that rule is pinned by
    test_relay_publish_stale_connected.py's `untrusted` case, which configures no home)."""
    assert result["rebuilt"], "a socket that stopped answering was never replaced"
    assert result["resent"], "the pending event was not re-sent on the replacement socket"
    assert result["silent"] is True
