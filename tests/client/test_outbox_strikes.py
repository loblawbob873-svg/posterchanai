"""A queued post is only given up on when the relay REFUSED it — never when it went quiet.

WHAT HAPPENED. `Relay.publish` gives the relay 8 seconds to acknowledge a note. While a DM restore
was saturating the same socket, that window was missed; each miss counted as a strike, and at five
strikes the Outbox deleted the event and the client removed its local copy. The user watched a post
sit marked "Pending" and then vanish — and it had been ACCEPTED BY THE RELAY the whole time. It was
still there, published, minutes later: only the acknowledgement was late.

MAX_TRIES exists for an event the relay will never take — a bad signature, a blocked author, the
wrong kind. That is information. A relay that did not answer is weather, and the difference decides
whether somebody's writing is thrown away.

  refusal-strikes   a relay that says no five times ends the retry, as it always has
  silence-does-not  a timeout leaves the entry and its budget untouched — MAX_AGE is the backstop
  no-silent-loss    a drop still reports itself, and never happens on a quiet relay

Drives the SHIPPED outbox.js under node with a stubbed relay and localStorage.
"""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUTBOX = ROOT / "static" / "js" / "client" / "outbox.js"

DRIVER = """
const store = {};
global.localStorage = {
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: k => { delete store[k]; },
};
let result = { ok: false, msg: 'timeout' };
let published = 0;
global.window = {
  addEventListener(){},
  Relay: { status: 'ok', async publish(){ published++; return result; } },
};
global.self = global.window;
// flush() reads the BARE global `Relay` (a browser page has one); node needs it hung on globalThis
// as well as on the window stub the module writes itself into.
global.Relay = global.window.Relay;

require(process.argv[2]);
const Outbox = global.window.Outbox;

(async () => {
  const out = {};
  // `sig` is required: the Outbox only ever holds SIGNED events (that is what makes re-sending one
  // a no-op at the relay rather than a second post).
  const ev = { id: 'a'.repeat(64), kind: 1, content: 'hello', sig: 'f'.repeat(128),
               created_at: Math.floor(Date.now()/1000) };

  // 1. TEN quiet failures: the post must still be there.
  Outbox.add(ev);
  result = { ok: false, msg: 'timeout' };
  for (let i = 0; i < 10; i++) await Outbox.flush();
  out.afterTimeouts = Outbox.count();
  // Read the queue back through its own storage key: `tries` is the give-up budget and it is the
  // number this test is about, so it is asserted directly rather than inferred.
  // Tolerant of the entry being GONE, so the pre-fix behaviour fails as a readable assertion
  // ("the post was dropped by timeouts alone") instead of a TypeError.
  out.triesSpent = (JSON.parse(store['pc_outbox'] || '[]')[0] || {}).tries ?? -1;

  // 2. …and it goes out the moment the relay answers.
  result = { ok: true };
  const r = await Outbox.flush();
  out.sentAfterRecovery = r.sent;
  out.leftAfterRecovery = Outbox.count();

  // 3. A relay that REFUSES still ends the retry, and says so.
  const ev2 = { id: 'b'.repeat(64), kind: 1, content: 'nope', sig: 'e'.repeat(128),
                created_at: Math.floor(Date.now()/1000) };
  Outbox.add(ev2);
  result = { ok: false, msg: 'blocked: not a web-of-trust member' };
  let dropped = [];
  for (let i = 0; i < 6; i++) { const x = await Outbox.flush(); dropped = dropped.concat(x.dropped); }
  out.refusedDropped = dropped.length;
  out.afterRefusals = Outbox.count();

  console.log(JSON.stringify(out));
})();
"""


@unittest.skipIf(shutil.which("node") is None, "node not installed")
class OutboxStrikeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.mkdtemp(prefix="pcob-")
        drv = os.path.join(cls.tmp, "drv.js")
        with open(drv, "w") as fh:
            fh.write(DRIVER)
        r = subprocess.run(["node", drv, str(OUTBOX)], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError(r.stderr[-2000:])
        cls.r = json.loads(r.stdout.strip().splitlines()[-1])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_quiet_relay_never_costs_a_post(self):
        """THE BUG: five 8-second timeouts during a busy moment deleted a post the relay had
        already accepted."""
        self.assertEqual(self.r["afterTimeouts"], 1, "the post was dropped by timeouts alone")
        self.assertEqual(self.r["triesSpent"], 0,
                         "a timeout spent part of the give-up budget")

    def test_it_goes_out_when_the_relay_comes_back(self):
        self.assertEqual(self.r["sentAfterRecovery"], 1)
        self.assertEqual(self.r["leftAfterRecovery"], 0)

    def test_a_relay_that_refuses_still_ends_the_retry(self):
        """The case MAX_TRIES was written for, and it must keep working: an event this relay will
        never accept should not be retried for a week."""
        self.assertEqual(self.r["refusedDropped"], 1, "a genuinely refused event was retried for ever")
        self.assertEqual(self.r["afterRefusals"], 0)


if __name__ == "__main__":
    unittest.main()
