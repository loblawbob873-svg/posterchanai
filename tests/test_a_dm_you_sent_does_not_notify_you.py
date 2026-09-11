"""A message you SENT is not news to the device that sent it.

Reported: "evey time I send a DM i get a push notification."

NIP-17 publishes TWO gift wraps for one message — one the peer can open and a SELF-COPY the sender
can, so their other devices see what they sent. Both are p-tagged to their own reader, and a gift
wrap's author is an ephemeral throwaway key, so the push watcher's "don't notify the author" test
(`pk != author`) cannot see that the self-copy's recipient IS its sender.

The device that published it is the only party that knows. It records the wrap ids and drops a push
carrying one — an EXACT match. A time window after sending was the obvious alternative and is the
wrong one: it would silence a real DM that arrived in the gap, which this subsystem already argues
is worse than a duplicate.
"""
from pathlib import Path
import re
import subprocess
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / "mobile/android/app/src/main/java/place/poster/app/push"
NOTIFIED = (JAVA / "ClientNotified.java").read_text(encoding="utf-8")
DELIVER = (JAVA / "PushEventService.java").read_text(encoding="utf-8")
PLUGIN = (JAVA / "PushPlugin.java").read_text(encoding="utf-8")
SERVER = (ROOT / "app/services/nostr_push_service.py").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")

HARNESS = r"""
import place.poster.app.push.ClientNotified;
public class H {
  static int bad = 0;
  static void ok(String what, boolean cond){ if(!cond){ bad++; System.out.println("FAIL "+what); } }
  public static void main(String[] a){
    long t = 1_000_000L;
    ClientNotified.clear();
    ok("an id we never recorded is shown", !ClientNotified.weSent("aa", t));
    ClientNotified.sent("aa", t);
    ok("an id we published is dropped", ClientNotified.weSent("aa", t + 500));
    ok("and only once — a push arrives once", !ClientNotified.weSent("aa", t + 600));
    ClientNotified.sent("bb", t);
    ok("a stale id is not evidence", !ClientNotified.weSent("bb", t + ClientNotified.SENT_WINDOW_MS + 1));
    ClientNotified.sent("cc", t);
    ok("a backwards clock does not make it fresh", !ClientNotified.weSent("cc", t - 1));
    ok("an empty id is never a match", !ClientNotified.weSent("", t));
    ok("a null id is never a match", !ClientNotified.weSent(null, t));
    ClientNotified.sent(null, t); ClientNotified.sent("", t);   // must not throw
    for (int i = 0; i < 400; i++) ClientNotified.sent("id" + i, t);
    ok("the map is bounded", !ClientNotified.weSent("id0", t + 1));
    ok("and keeps the newest", ClientNotified.weSent("id399", t + 1));
    ClientNotified.clear();
    ok("clear forgets them", !ClientNotified.weSent("id399", t + 1));
    System.out.println(bad == 0 ? "ALL OK" : "FAILURES " + bad);
  }
}
"""


@pytest.mark.skipif(not (shutil.which("javac") and shutil.which("java")), reason="no JDK")
def test_the_guard_runs(tmp_path):
    """Pure Java, so the rules are EXECUTED and not read."""
    src = tmp_path / "place/poster/app/push"
    src.mkdir(parents=True)
    (src / "ClientNotified.java").write_text(NOTIFIED, encoding="utf-8")
    (tmp_path / "H.java").write_text(HARNESS, encoding="utf-8")
    build = subprocess.run(["javac", "-d", str(tmp_path), str(src / "ClientNotified.java"),
                            str(tmp_path / "H.java")], capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    run = subprocess.run(["java", "-cp", str(tmp_path), "H"], capture_output=True, text=True)
    assert "ALL OK" in run.stdout, run.stdout + run.stderr


def test_the_server_carries_the_wrap_id_as_a_dedup_key_only():
    dm = SERVER.split('"type": "dm"', 1)[1][:400]
    assert '"wid"' in dm, dm
    # It must not become a deep link: PushEventService routes on `eid`/`view`, never on this.
    # The RULE is "route is never derived from wid", not "the word route is far away from it".
    assert not re.search(r"route\s*=[^;\n]*\bwid\b", DELIVER), (
        "wid is a dedup key; routing on it would deep-link to a gift wrap, which opens nothing")


def test_the_delivery_drops_only_a_dm_and_only_by_id():
    line = next(ln for ln in DELIVER.splitlines() if "weSent(" in ln)
    assert '"dm".equals(type)' in line, line
    assert "wid" in line, line


def test_the_sender_records_both_wraps_before_they_are_published():
    """The self-copy is the one that comes back, but the peer's is recorded too: a second device of
    the same account can be the one that publishes, and neither should notify its own publisher."""
    # Sliced to a real BOUNDARY, not a character count. This read the first 1400 characters of
    # sendDm, so adding a comment to the code it tests moved the call out of the window and failed
    # a test about something else entirely.
    send = APP.split("async function sendDm(", 1)[1]
    send = send[:send.index("Store.saveEvent") + 40]
    assert "notePublished" in send, send[:400]
    assert "toSelf&&toSelf.id" in send and "toPeer&&toPeer.id" in send, send[:400]
    assert send.index("notePublished") < send.index("Store.saveEvent"), (
        "record it before anything can await — the push can arrive while this function is still in "
        "flight")


def test_the_plugin_exposes_it():
    assert "public void notePublished(PluginCall call)" in PLUGIN
    assert "@PluginMethod" in PLUGIN.split("notePublished", 1)[0][-120:]


def test_it_still_fails_open():
    """Nothing is recorded on a phone whose WebView is not running — which is exactly the phone the
    push exists for — so no record must mean SHOW."""
    body = NOTIFIED.split("public static boolean weSent(", 1)[1].split("\n    }", 1)[0]
    assert "return false;" in body
    assert re.search(r"when == null\)\s*return false", body), body
