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
let revived = 0;
const documentListeners = {};
global.document = {
  hidden: false,
  addEventListener(type, fn){ (documentListeners[type] ||= []).push(fn); },
};
global.window = {
  addEventListener(){},
  Relay: { status: 'ok', reviveStale(){ revived++; }, async publish(){ published++; return result; } },
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

  // 4. A queued event cannot depend on a future relay status transition: a resumed phone can have
  // an `ok` pool containing a dead socket. add() itself must wake/retry it.
  const ev3 = { id: 'c'.repeat(64), kind: 1, content: 'resume', sig: 'd'.repeat(128),
                created_at: Math.floor(Date.now()/1000) };
  result = { ok: true };
  Outbox.add(ev3);
  await new Promise(resolve => setTimeout(resolve, 1700));
  out.autoRetried = Outbox.count() === 0;
  out.reviveCalls = revived;

  // 5. Discard is a local operation. It must work even when the pool claims `ok` but cannot carry
  // a publish—the stale-status state that made the UI force Retry forever.
  result = { ok: false, msg: 'offline' };
  const ev4 = { id: 'd'.repeat(64), kind: 1, content: 'discard', sig: 'c'.repeat(128),
                created_at: Math.floor(Date.now()/1000) };
  Outbox.add(ev4);
  const beforeDiscardPublishes = published;
  out.localDiscard = Outbox.remove(ev4.id) && Outbox.count() === 0;
  out.discardPublished = published - beforeDiscardPublishes;

  // 6. The retry timer can expire while Android has frozen/hidden the WebView. It correctly avoids
  // publishing in the background, but foregrounding must replace that consumed attempt even when
  // the relay pool remains labelled `ok` and therefore emits no fresh status transition.
  const ev5 = { id: 'e'.repeat(64), kind: 7, content: '+', sig: 'b'.repeat(128),
                created_at: Math.floor(Date.now()/1000) };
  document.hidden = true;
  result = { ok: true };
  Outbox.add(ev5);
  await new Promise(resolve => setTimeout(resolve, 1700));
  out.heldWhileHidden = Outbox.count() === 1;
  document.hidden = false;
  for(const fn of documentListeners.visibilitychange || []) fn();
  await new Promise(resolve => setTimeout(resolve, 500));
  out.sentOnForeground = Outbox.count() === 0;

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

    def test_an_ok_but_stale_phone_connection_retries_without_a_status_change(self):
        self.assertTrue(self.r["autoRetried"])
        self.assertGreaterEqual(self.r["reviveCalls"], 1)

    def test_discard_never_needs_the_relay(self):
        self.assertTrue(self.r["localDiscard"])
        self.assertEqual(self.r["discardPublished"], 0)

    def test_a_reaction_whose_retry_expired_in_background_sends_on_foreground(self):
        self.assertTrue(self.r["heldWhileHidden"],
                        "the hidden WebView attempted to publish in the background")
        self.assertTrue(self.r["sentOnForeground"],
                        "foregrounding did not replace the consumed retry timer")

    def test_resume_readiness_also_drains_without_a_status_transition(self):
        app = (ROOT / "static" / "js" / "client" / "app.js").read_text()
        start = app.index("function _resumeRelay(){")
        body = app[start:app.index("function _nativeResume", start)]
        self.assertIn("if(ok){ _reaskMissing(); _flushOutbox(); }", body)


if __name__ == "__main__":
    unittest.main()
