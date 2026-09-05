"""A DEAD IPC SOCKET IS HANDED OUT UNTIL `close` FIRES, AND EVERY WRITE TO IT THROWS.

`_connect()` returns `this.sock` whenever it is set, and that field is cleared only by the socket's
`close` event. Between the peer going away and that event arriving, every request is written to a
corpse. MEASURED on the real desk:

    Error: EPIPE ... at /opt/posterchan/resources/app.asar/wm-wayfire.js:146
    errno: -32, code: 'EPIPE', syscall: 'write'

after which nothing the shell asked of the compositor worked again -- reported as "two windows on
one monitor are stuck and not moveable". The re-watch that fixes a LOST SUBSCRIPTION does not cover
this: it is armed from `close`, which is precisely the event that has not happened yet.

These tests RUN the shipped class against a real Unix socket that is killed underneath it, because
the failure is a race between two socket events and no amount of reading the file shows it.
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
WM = ROOT / "desktop/wm-wayfire.js"


def _run(script: str):
    tmp = tempfile.mkdtemp()
    try:
        path = Path(tmp, "t.js")
        path.write_text(script.replace("__WM__", str(WM)))
        out = subprocess.run(["node", str(path)], capture_output=True, text=True, timeout=120)
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


HARNESS = """
const net = require('net');
const fs = require('fs');
const os = require('os');
const path = require('path');
const {WayfireWM} = require('__WM__');

// A server that speaks the framing well enough to answer, and can be told to die.
function serve(sock, onConn){
  const srv = net.createServer(c => onConn(c));
  srv.listen(sock);
  return srv;
}
function frame(obj){
  const b = Buffer.from(JSON.stringify(obj));
  const h = Buffer.alloc(4); h.writeUInt32LE(b.length, 0);
  return Buffer.concat([h, b]);
}
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'wfipc-'));
const sock = path.join(dir, 'wayfire-0.socket');
"""


class TestARequestSurvivesTheSocketDying(unittest.TestCase):
    def test_a_write_into_a_socket_that_has_not_reported_death_yet_still_works(self):
        """THE ACTUAL RACE, and the reason a gentler version of this test proves nothing.

        Wait for `close` and the old code passes: the event clears `this.sock` and the next request
        reconnects by itself. The failure needs the write to happen INSIDE the window between the
        socket dying and `close` being delivered -- which is the ordinary case when a compositor
        goes away mid-session, and is where the EPIPE in the real log came from. So the socket is
        destroyed and the request issued with no delay at all."""
        out = _run(HARNESS + """
let conns = 0;
let srv = serve(sock, c => {
  conns++;
  c.on('data', () => c.write(frame({ok: true, conn: conns})));
});
const wm = new WayfireWM(sock, {});
(async () => {
  await wm._send('probe', {});                 // establishes connection 1
  wm.sock.destroy();                           // dead, but `close` has NOT been delivered yet
  const again = await wm._send('probe', {});   // same tick: this is the write that used to EPIPE
  console.log(JSON.stringify({conns, ok: !!(again && again.ok)}));
  srv.close(); process.exit(0);
})().catch(e => { console.log(JSON.stringify({error: String(e && e.message || e)})); process.exit(0); });
""")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertNotIn("error", got, f"the second request failed: {got}")
        self.assertTrue(got["ok"], got)
        self.assertGreaterEqual(got["conns"], 2, "it reused the dead socket instead of reconnecting")

    def test_it_does_not_retry_for_ever_when_the_compositor_is_gone(self):
        """A genuinely absent compositor must surface as a failure, not a reconnect loop."""
        out = _run(HARNESS + """
const wm = new WayfireWM(path.join(dir, 'nothing-here.socket'), {});
let tries = 0;
const realConnect = net.createConnection;
net.createConnection = function(...a){ tries++; return realConnect.apply(net, a); };
wm._send('probe', {}).then(
  () => console.log(JSON.stringify({resolved: true, tries})),
  () => console.log(JSON.stringify({rejected: true, tries}))
).then(() => process.exit(0));
""")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertTrue(got.get("rejected"), f"a missing compositor did not surface as a failure: {got}")
        self.assertLessEqual(got["tries"], 4, f"it kept reconnecting: {got}")


class TestTheReplyQueueStaysInStep(unittest.TestCase):
    def test_a_failed_write_leaves_no_pending_entry_behind(self):
        """Replies are matched by ORDER. An entry left behind after a write that never went out
        shifts every later reply by one -- the geometry of one window answering for another."""
        out = _run(HARNESS + """
const wm = new WayfireWM(sock, {});
let srv = serve(sock, c => { c.on('data', () => c.write(frame({ok: true}))); });
(async () => {
  await wm._send('probe', {});
  // Kill the socket and write straight into it, the way a queued request would.
  const dead = wm.sock;
  dead.destroy();
  let threw = false;
  try { await wm._send('probe', {}); } catch (_) { threw = true; }
  await new Promise(r => setTimeout(r, 50));
  console.log(JSON.stringify({pending: wm.pending.length, threw}));
  srv.close(); process.exit(0);
})().catch(e => { console.log(JSON.stringify({error: String(e && e.message || e)})); process.exit(0); });
""")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertEqual(got.get("pending"), 0, f"a stale reply slot was left in the queue: {got}")
        self.assertFalse(got.get("threw"),
                         "a request issued before `close` arrived failed instead of reconnecting")


class TestOnlyUnsentRequestsAreRepeated(unittest.TestCase):
    def test_a_compositor_error_is_not_sent_twice(self):
        """`subscribe()` NEGOTIATES its event list by sending a watch and reading the refusal, so a
        blanket retry doubles every legitimate error and halves the negotiation's speed. And a
        request that DID go out before the socket closed is not repeatable either: nothing here can
        tell whether the compositor applied it, and re-sending a geometry or a close is not free."""
        out = _run(HARNESS + """
let seen = 0;
let srv = serve(sock, c => {
  c.on('data', () => { seen++; c.write(frame({error: 'no such event'})); });
});
const wm = new WayfireWM(sock, {});
wm._send('window-rules/events/watch', {events: ['nope']}).then(
  () => console.log(JSON.stringify({resolved: true, seen})),
  () => console.log(JSON.stringify({rejected: true, seen}))
).then(() => { srv.close(); process.exit(0); });
""")
        self.assertEqual(out.returncode, 0, out.stderr)
        got = json.loads(out.stdout.strip().splitlines()[-1])
        self.assertTrue(got.get("rejected"), got)
        self.assertEqual(got.get("seen"), 1,
                         f"the compositor was asked twice for a request it had already answered: {got}")


class TestTheWatchIsReArmedWhenTheSocketIsDropped(unittest.TestCase):
    def test_dropping_a_socket_asks_for_the_watch_back(self):
        """A socket destroyed by the write path may never emit `close`, and `close` is what the
        existing re-watch hangs off. A shell with no window events is this class's worst state."""
        body = WM.read_text(encoding="utf-8")
        drop = body[body.index("_dropSocket(s){"):]
        drop = drop[: drop.index("\n  _send(")]
        self.assertIn("_watchLost", drop,
                      "a dropped socket does not re-arm the window-event watch")


if __name__ == "__main__":
    unittest.main()
