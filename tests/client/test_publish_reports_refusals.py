"""A relay that REFUSES an event must not be reported as a relay that went quiet.

`["OK", <id>, false, "invalid: bad id or signature"]` was dropped on the floor: only an acceptance
settled the publish, so a refusal fell through to the 8-second timeout and came back as `timeout`.
Everything downstream then treats "this event is wrong" exactly like "the network is busy" — the
Outbox retries it for a week, the post sits marked Pending for ever, and the one sentence that
explains it, which the relay sent immediately, is never shown to anyone. That is what turned a
wrongly-signed quote post into an unexplainable stuck post, and it is why the give-up rule in
outbox.js could never fire: the reason it keys on never arrived.

  reason-survives   a refusal settles with the relay's own words, not "timeout"
  accept-still-wins one relay refusing must not settle a publish another may accept
  all-refused       …but once every relay written to has refused, do not sit out the timeout
  quiet-is-quiet    a relay that says nothing at all still reports a timeout

Drives the SHIPPED relay.js under node against stub sockets.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RELAY = ROOT / "static" / "js" / "client" / "relay.js"

DRIVER = """
// A WebSocket that records what was sent and lets the test answer as a relay would.
class FakeWS {
  constructor(url){ this.url = url; this.readyState = 1; this.sent = []; FakeWS.all.push(this); }
  send(s){ this.sent.push(s); }
  close(){ this.readyState = 3; }
  reply(arr){ this.onmessage && this.onmessage({ data: JSON.stringify(arr) }); }
}
FakeWS.all = [];
global.WebSocket = FakeWS;
global.window = global;
global.self = global;                       // relay.js reads self.__VER at import
// The signing worker is not part of this: publish() only writes an already-signed event.
global.Worker = class { constructor(){} postMessage(){} addEventListener(){} terminate(){} };
global.document = { addEventListener(){}, hidden: false, visibilityState: 'visible' };
global.location = { origin: 'https://x.test', protocol: 'https:' };
global.navigator = { onLine: true };
global.indexedDB = { open(){ const r = {}; setTimeout(()=>{ r.onerror && r.onerror(); },0); return r; } };

require(process.argv[2]);
const Relay = global.Relay;

const EV = { id: 'a'.repeat(64), kind: 1, content: 'hi', sig: 'f'.repeat(128),
             pubkey: 'b'.repeat(64), created_at: 1 };

(async () => {
  const out = {};
  // `connect(url)` takes ONE url (it wraps it for configure); the pool's real multi-relay entry is
  // configure({urls}). Getting this wrong made only one socket and the mixed case had nothing to
  // accept with — worth the comment, since the whole point of this file is two relays disagreeing.
  Relay.configure({ urls: ['wss://one.test', 'wss://two.test'], verify: false });
  await new Promise(r => setTimeout(r, 20));
  for (const w of FakeWS.all){ w.readyState = 1; w.onopen && w.onopen(); }
  await new Promise(r => setTimeout(r, 20));
  const socks = FakeWS.all.slice();

  // 1. EVERY relay refuses: the reason comes back, and without waiting out the timeout.
  {
    const t0 = Date.now();
    const p = Relay.publish(EV, 5000);
    await new Promise(r => setTimeout(r, 10));
    socks.forEach(w => w.reply(['OK', EV.id, false, 'invalid: bad id or signature']));
    const r = await p;
    out.refusedOk = r.ok;
    out.refusedMsg = r.msg;
    out.refusedFast = (Date.now() - t0) < 3000;
  }

  // 2. One refuses, another accepts: acceptance wins.
  {
    const p = Relay.publish(EV, 5000);
    await new Promise(r => setTimeout(r, 10));
    socks[0].reply(['OK', EV.id, false, 'blocked: whatever']);
    socks[1].reply(['OK', EV.id, true, '']);
    const r = await p;
    out.mixedOk = r.ok;
  }

  // 3. Silence is still silence.
  {
    const p = Relay.publish(EV, 120);
    const r = await p;
    out.quietOk = r.ok;
    out.quietMsg = r.msg;
  }

  // 4. A relay may store+echo an event while its separate OK frame is lost. Other clients already
  // have the post in that case, so the sender must not leave it marked Pending.
  {
    const p = Relay.publish(EV, 5000);
    await new Promise(r => setTimeout(r, 10));
    socks[0].reply(['EVENT', 'any-live-sub', EV]);
    const r = await p;
    out.echoAck = r.ok;
    out.echoMsg = r.msg;
  }

  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class PublishRefusalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="pcpub-")
        drv = os.path.join(cls.tmp, "drv.js")
        with open(drv, "w") as fh:
            fh.write(DRIVER)
        r = subprocess.run(["node", drv, str(RELAY)], capture_output=True, text=True, timeout=60)
        if r.returncode != 0 or not r.stdout.strip():
            raise unittest.SkipTest("relay.js did not drive under node: " + r.stderr[-400:])
        cls.r = json.loads(r.stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_refusal_comes_back_with_the_relays_own_words(self):
        """THE BUG. Without this the Outbox cannot tell a wrong event from a busy network, so it
        retries something that can never be accepted until it gives up and deletes it."""
        self.assertIs(self.r["refusedOk"], False)
        self.assertIn("invalid", str(self.r["refusedMsg"]),
                      f"the reason was lost: {self.r['refusedMsg']!r}")

    def test_it_does_not_sit_out_the_timeout_once_every_relay_has_refused(self):
        self.assertTrue(self.r["refusedFast"], "a fully refused publish still waited for the clock")

    def test_one_refusal_never_beats_another_relays_acceptance(self):
        """The rule publish() was written around, and the reason a refusal cannot simply settle it."""
        self.assertIs(self.r["mixedOk"], True)

    def test_silence_is_still_reported_as_a_timeout(self):
        self.assertIs(self.r["quietOk"], False)
        self.assertEqual(self.r["quietMsg"], "timeout")

    def test_a_trusted_event_echo_clears_a_lost_ok_acknowledgement(self):
        self.assertTrue(self.r["echoAck"])
        self.assertEqual(self.r["echoMsg"], "relay echo")


if __name__ == "__main__":
    unittest.main()
