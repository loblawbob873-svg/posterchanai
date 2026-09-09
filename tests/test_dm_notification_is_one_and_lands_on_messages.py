"""A DM notification opens MESSAGES, and there is exactly ONE of it.

    "clicking [a] message / dm notification does not bring me to the DM screen!"
    "i do not want 2 notifications on my phone for every DM!
     'someone sent you a message' then 'bla bla bla sent you a message'"

Two reports, one payload. `PushEventService.deliver` derives its deep link from `eid` (a post) or
`view` (a screen) and falls back to "notifications" when neither is there — and a DM push carried
NEITHER, because a NIP-17 gift wrap has no post to open and nothing named a screen. So every DM
notification ever sent by this node opened Notifications. It was invisible because Notifications is
a real screen: nothing threw, nothing logged, the app just came forward on the wrong thing.

The second report is the same payload from the other end. The server cannot decrypt a gift wrap, so
its push says "Someone"; a running client can, so it says who. Both are wanted — the push is the
only copy that reaches a phone with the app closed, the client's is the only one that names the
sender — so they SHARE AN IDENTITY instead of one being deleted. Android keys a notification on
(tag, id): with the push posting under the default "msg" and the client under "pc-dm" they stacked;
with one tag the named one REPLACES the blind one.

The other ordering needs the device: when the client got there first, a shared tag would let the
late generic push overwrite the better wording, or re-post a message already read. `ClientNotified`
is that half, and it FAILS OPEN — no record means show, because a phone whose WebView is not running
is exactly the phone the push exists for.

Three halves, because each one passes while the others are broken:
  * the SERVER payload is built by running `_dm_handler`, not by grepping it;
  * `ClientNotified` is RUN (javac + java), including the fail-open and backwards-clock rules;
  * `PushEventService` is checked for consulting both.
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / "mobile/android/app/src/main/java/place/poster/app"
PUSH = JAVA / "push"

HAVE_JDK = shutil.which("javac") and shutil.which("java")

sys.path.insert(0, str(ROOT))


class TheServerPushSaysWhichScreenAndWhichNotification(unittest.TestCase):
    """RUN `_dm_handler` and read the payload it hands the transport."""

    def _payload(self):
        from app.services import nostr_push_service as nps

        sent = []

        async def subscriber_pks():
            return {"bob" * 21 + "b"}

        def subs_for(pks):
            return {pk: [{"endpoint": "pcdirect:dev"}] for pk in pks}

        async def name_for(pk):
            return ""

        def send(sub, payload):
            sent.append(payload)
            return True

        old = (nps._subscriber_pks, nps._subs_for, nps._name_for,
               nps.push_service.send, dict(nps._dm_recent))
        nps._subscriber_pks = subscriber_pks
        nps._subs_for = subs_for
        nps._name_for = name_for
        nps.push_service.send = send
        nps._dm_recent.clear()
        try:
            wrap = {"kind": 1059, "pubkey": "ee" * 32,
                    "tags": [["p", "bob" * 21 + "b"]], "content": "<sealed>"}
            asyncio.run(nps._dm_handler(wrap))
        finally:
            (nps._subscriber_pks, nps._subs_for, nps._name_for, nps.push_service.send, _) = old
            nps._dm_recent.clear()
        self.assertEqual(len(sent), 1, "the DM push was not sent at all")
        return sent[0]

    def test_it_names_the_messages_screen(self):
        """Without `view`, PushEventService.deliver deep-links to "notifications"."""
        self.assertEqual(self._payload().get("view"), "messages")

    def test_it_carries_the_clients_own_tag(self):
        """Same (tag, id) as the client's decrypted notification → replace, not stack."""
        self.assertEqual(self._payload().get("tag"), "pc-dm")

    def test_it_is_still_typed_dm(self):
        """The device filter and the toggle both key on this; renaming it silences the wrong thing."""
        self.assertEqual(self._payload().get("type"), "dm")


CLIENT_NOTIFIED_HARNESS = r"""
import place.poster.app.push.ClientNotified;

public class Harness {
    static int failed = 0;
    static void ok(String what, boolean cond) {
        if (!cond) { failed++; System.out.println("FAIL " + what); }
        else System.out.println("ok   " + what);
    }

    public static void main(String[] a) {
        long t = 1_000_000_000L;

        // FAIL OPEN. A phone whose WebView never ran has no record, and that phone is the whole
        // reason the push exists. This is the rule that must never invert.
        ClientNotified.clear();
        ok("no record means show", !ClientNotified.recentlyDm(t));

        // The client just drew its own, named notification. The generic push behind it is a
        // duplicate.
        ClientNotified.dm(t);
        ok("a fresh client notification speaks for the push", ClientNotified.recentlyDm(t + 5));

        // ...but not for ever. A WebView that died seconds after notifying must not silence the
        // next hour of messages.
        ok("the window closes", !ClientNotified.recentlyDm(t + ClientNotified.WINDOW_MS));
        ClientNotified.dm(t);
        ok("just inside the window still speaks",
           ClientNotified.recentlyDm(t + ClientNotified.WINDOW_MS - 1));

        // A clock that went backwards must not latch this open — latched, it suppresses every DM
        // notification for as long as the condition lasts, which is worse than the duplicate.
        ClientNotified.dm(t);
        ok("a backwards clock shows rather than latches", !ClientNotified.recentlyDm(t - 10));

        ClientNotified.dm(t);
        ClientNotified.clear();
        ok("clear forgets it", !ClientNotified.recentlyDm(t + 1));

        // It is a WINDOW, not a one-shot: a second push inside it is still a duplicate of the same
        // conversation, and reading must not consume the record.
        ClientNotified.dm(t);
        ClientNotified.recentlyDm(t + 1);
        ok("reading does not consume it", ClientNotified.recentlyDm(t + 2));

        if (failed > 0) { System.out.println("FAILED " + failed); System.exit(1); }
        System.out.println("ALL OK");
    }
}
"""


@unittest.skipUnless(HAVE_JDK, "javac/java not installed")
class ClientNotifiedRuns(unittest.TestCase):
    def test_the_rules_hold_when_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "place/poster/app/push"
            pkg.mkdir(parents=True)
            shutil.copy(PUSH / "ClientNotified.java", pkg / "ClientNotified.java")
            (Path(tmp) / "Harness.java").write_text(CLIENT_NOTIFIED_HARNESS)
            c = subprocess.run(["javac", "-d", tmp, str(pkg / "ClientNotified.java"),
                                str(Path(tmp) / "Harness.java")],
                               capture_output=True, text=True, cwd=tmp)
            self.assertEqual(c.returncode, 0, c.stderr)
            r = subprocess.run(["java", "-cp", tmp, "Harness"],
                               capture_output=True, text=True, cwd=tmp)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TheRendererHonoursBoth(unittest.TestCase):
    body = (PUSH / "PushEventService.java").read_text()

    def test_an_explicit_tag_wins_over_the_event_id(self):
        """A gift wrap has no eid, so without this a DM can never share the client's tag."""
        self.assertIn('j.optString("tag"', self.body)

    def test_a_dm_the_client_already_showed_is_not_shown_again(self):
        self.assertIn("ClientNotified.recentlyDm", self.body)

    def test_the_suppression_is_dm_only(self):
        """A like or a call must never be silenced by somebody having received a DM."""
        self.assertIn('"dm".equals(type) && ClientNotified.recentlyDm', self.body)

    def test_the_client_records_its_own(self):
        """Without the record, the second ordering (client first) still double-notifies."""
        plugin = (PUSH / "PushPlugin.java").read_text()
        self.assertIn("ClientNotified.dm(", plugin)


class TheClientSharesTheIdentity(unittest.TestCase):
    app = (ROOT / "static/js/client/app.js").read_text()

    def test_the_dm_notification_is_typed(self):
        """The native side records the client's DM by type/tag; an untyped one records nothing."""
        line = [l for l in self.app.splitlines() if "sent you a DM" in l and "osNotify" in l]
        self.assertTrue(line, "the DM notification call moved — re-read this test")
        self.assertIn("type:'dm'", line[0])
        self.assertIn("tag:'pc-dm'", line[0])

    def test_the_service_worker_honours_an_explicit_tag(self):
        """The PWA half of the same collapse — a gift wrap has no eid to key on."""
        self.assertIn("d.tag || d.eid", (ROOT / "static/js/client/sw.js").read_text())


if __name__ == "__main__":
    unittest.main()
