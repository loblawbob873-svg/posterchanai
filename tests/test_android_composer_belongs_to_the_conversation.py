"""The native Texts composer must not carry one person's message into another's conversation.

Reported as the text input field containing the message of a different, incoming message.

ThreadActivity is `launchMode="singleTop"` and SmsNotifier opens it with FLAG_ACTIVITY_CLEAR_TOP.
So tapping the notification for an incoming text while that screen is ALREADY open does not build a
new screen — Android delivers `onNewIntent`, which calls `readIntent`. That moved `address` and
`threadId` and reloaded the message list while leaving the compose box exactly as the person had
typed it, for somebody else. `send()` reads the CURRENT `address`, so the next tap of Send would
have delivered a private reply to the wrong number. Nothing logs, and the screen looks right: the
conversation on screen IS the one that was tapped.

The rule is the one the picture draft already had: a draft is filed under the conversation it was
typed in (`MmsDraft`, keyed by address). Hand-over parks what is typed and shows what that
conversation has waiting.

The decision is RUN here rather than grepped. `readIntent` is where the conversation can change
under the composer, and what matters is the ORDER of three things inside it — park/restore, the
`?body=` prefill, and the assignment to `address` — which no regex can answer. The shipped method is
compiled with fakes for the pieces it reaches out to, so the sequence under test is the real one.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SMS = ROOT / "mobile/android/app/src/main/java/place/poster/app/sms"
THREAD = (SMS / "ThreadActivity.java").read_text(encoding="utf-8")
MANIFEST = (ROOT / "mobile/android/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")
NOTIFIER = (SMS / "SmsNotifier.java").read_text(encoding="utf-8")


def _method(src, opener):
    """One method, by brace counting from its opening line."""
    i = src.index(opener)
    depth, j, started = 0, i, False
    while j < len(src):
        if src[j] == "{":
            depth += 1
            started = True
        elif src[j] == "}":
            depth -= 1
            if started and depth == 0:
                return src[i:j + 1]
        j += 1
    raise AssertionError("could not bound " + opener)


def test_the_notification_can_reach_an_open_conversation_without_a_new_screen():
    """The precondition for the whole bug — if either half changes, this stops being a hand-over.

    singleTop + CLEAR_TOP at an activity that is already on top is delivered through onNewIntent.
    """
    entry = MANIFEST[MANIFEST.index(".sms.ThreadActivity"):]
    entry = entry[:entry.index("/>")]
    assert 'android:launchMode="singleTop"' in entry
    assert "FLAG_ACTIVITY_CLEAR_TOP" in NOTIFIER
    assert "ThreadActivity.EXTRA_ADDRESS, from" in NOTIFIER, (
        "the notification no longer names the conversation it is about")
    body = _method(THREAD, "    protected void onNewIntent(Intent intent) {")
    assert "readIntent(intent)" in body, (
        "onNewIntent no longer re-reads the intent, so a hand-over there is not reached at all")


def test_a_sent_message_stops_being_a_draft_on_every_send_path():
    """Otherwise the next hand-over back into this conversation restores a message already sent."""
    for opener, name in (("    private void send() {", "sms"),
                         ("    private void sendMms(String body) {", "mms"),
                         ("    private void sendAsLink(", "link")):
        body = _method(THREAD, opener)
        if 'input.setText("")' not in body:
            continue
        assert 'MmsDraft.setText(' in body, (
            "the %s send path clears the box without retiring the stored draft" % name)


def test_a_backgrounded_screen_keeps_what_was_typed():
    body = _method(THREAD, "    protected void onPause() {")
    assert "MmsDraft.setText(this, address, input.getText().toString())" in body, (
        "a backgrounded activity can be destroyed outright; the draft has to outlive that")


def test_the_hand_over_runs(tmp_path):
    """Compile the SHIPPED readIntent/handOverComposer and drive conversation and share handoffs through them."""
    if shutil.which("javac") is None or shutil.which("java") is None:
        pytest.skip("no JDK")

    read_intent = _method(THREAD, "    private void readIntent(Intent i) {")
    hand_over = _method(THREAD, "    private void handOverComposer(String next) {")

    harness = r"""
package place.poster.app.sms;

public class ComposerHandOver {
  /* Only what readIntent touches. Everything else about the screen is irrelevant to the question
     "whose message is in the box", and a fake that answers more would be answering for the code. */
  static java.util.Map<String,String> prefs = new java.util.HashMap<String,String>();
  static class Box {
    String v = "";
    String getText(){ return v; }
    void setText(String s){ v = s == null ? "" : s; }
    void setSelection(int n){ }
  }
  Box input = new Box();
  String address = "";
  long threadId = 0;
  long[] threadIds = new long[0];
  Object attachment, capturedAttachment, attachmentDraft;
  void updateCount(){ }
  void paintAttachmentDraft(){ }
  // Attachment IO is outside this composer harness. Observe the handoff rather
  // than dropping the call: importing before address/prefill resolves is unsafe.
  int importCalls;
  String importedAddress, importedBody;
  void importSharedAttachment(Intent intent){
    importCalls++;
    importedAddress = address;
    importedBody = input.getText();
  }

%s

%s

  static void check(boolean ok, String why){ if(!ok) throw new AssertionError(why); }

  public static void main(String[] argv){
    /* 1. THE REPORT. Typing to one person, a text arrives from another, the notification is tapped:
          singleTop + CLEAR_TOP means this same screen is handed the new conversation. */
    ComposerHandOver s = new ComposerHandOver();
    s.readIntent(Intent.forThread("+15550111", 11));
    s.input.setText("the pin is 4821, do not tell anyone");
    s.readIntent(Intent.forThread("+15550222", 22));
    check(s.address.equals("+15550222"), "the screen did not move to the conversation tapped");
    check(s.input.getText().isEmpty(),
          "one person's message is in another person's composer: " + s.input.getText());

    /* 2. AND IT IS NOT DESTROYED, which is the other half — a guard that throws the draft away
          would pass the assertion above and lose somebody's work. */
    s.readIntent(Intent.forThread("+15550111", 11));
    check(s.input.getText().equals("the pin is 4821, do not tell anyone"),
          "the draft did not come back to the conversation it was typed in: " + s.input.getText());

    /* 3. A SECOND CONVERSATION KEEPS ITS OWN. */
    s.input.setText("running late");
    s.readIntent(Intent.forThread("+15550222", 22));
    check(s.input.getText().isEmpty(), "the second conversation was handed the first one's draft");
    s.input.setText("no problem");
    s.readIntent(Intent.forThread("+15550111", 11));
    check(s.input.getText().equals("running late"), "drafts crossed: " + s.input.getText());
    s.readIntent(Intent.forThread("+15550222", 22));
    check(s.input.getText().equals("no problem"), "drafts crossed: " + s.input.getText());

    /* 4. AN `sms:?body=` LINK STILL PREFILLS. The prefill only ever fills an EMPTY box, so a
          hand-over performed AFTER it would swallow the whole point of the parameter. */
    ComposerHandOver t = new ComposerHandOver();
    t.readIntent(Intent.forThread("+15550111", 11));
    t.input.setText("half a sentence");
    t.readIntent(Intent.link("+15550333", "here is the address"));
    check(t.address.equals("+15550333"), "the sms: link did not open its conversation");
    check(t.input.getText().equals("here is the address"),
          "the ?body= prefill was swallowed: " + t.input.getText());

    /* 5. A REDELIVERY OF THE SAME CONVERSATION IS NOT A HAND-OVER. onNewIntent also fires for the
          notification of a message from the person already on screen, and parking-then-restoring
          through storage there would be a silent round trip of the text under the cursor. */
    ComposerHandOver u = new ComposerHandOver();
    u.readIntent(Intent.forThread("+15550111", 11));
    u.input.setText("mid-word");
    u.readIntent(Intent.forThread("+15550111", 11));
    check(u.input.getText().equals("mid-word"), "the same conversation reset its own box");

    /* 6. A SHARED PHOTO'S CAPTION AND RECIPIENT MUST BE READY BEFORE ATTACHMENT IO.
          The picker/grant/staging implementation has its own runtime/device tests. */
    Intent share = Intent.forThread("+15550444", 44);
    share.body = "photo caption";
    u.readIntent(share);
    check(u.importedAddress.equals("+15550444"), "attachment import ran for the previous recipient");
    check(u.importedBody.equals("photo caption"), "attachment import ran before shared caption prefill");
    check(prefs.get("+15550111").equals("mid-word"), "sharing lost the old recipient's draft");
    int beforeNull = u.importCalls;
    u.readIntent(null);
    check(u.importCalls == beforeNull, "a missing intent triggered attachment import");

    System.out.println("composer hand-over cases passed");
  }
}

/* The fakes. */
class Intent {
  static final String EXTRA_TEXT = "android.intent.extra.TEXT";
  String address; long thread; String data; String body;
  static Intent forThread(String a, long t){ Intent i = new Intent(); i.address = a; i.thread = t; return i; }
  static Intent link(String a, String body){ Intent i = new Intent(); i.data = "sms:" + a; i.body = body; return i; }
  long getLongExtra(String k, long d){ return thread > 0 ? thread : d; }
  String getStringExtra(String k){ return address; }
  String getData(){ return data; }
  CharSequence getCharSequenceExtra(String k){ return body; }
  long[] getLongArrayExtra(String k){ return null; }
}
class SendTo {
  static boolean isMessageUri(String u){ return u != null && u.startsWith("sms:"); }
  static String numberFrom(String u){ return u == null ? "" : u.substring(4); }
  static String bodyFrom(String u){ return ComposerHandOver.LINK_BODY == null ? "" : ComposerHandOver.LINK_BODY; }
}
class SmsStore {
  static long threadIdFor(Object c, String a){ return 1; }
  static long[] idsFor(Object c, String a, long t){ return new long[]{ t }; }
}
class MmsDraft {
  static String text(Object ctx, String address){
    String v = ComposerHandOver.prefs.get(address);
    return v == null ? "" : v;
  }
  static void setText(Object ctx, String address, String body){
    if(address == null || address.isEmpty()) return;
    if(body == null || body.isEmpty()) ComposerHandOver.prefs.remove(address);
    else ComposerHandOver.prefs.put(address, body);
  }
}
""" % (read_intent.replace("private void readIntent", "void readIntent"),
       hand_over.replace("private void handOverComposer", "void handOverComposer"))

    # `?body=` is read off the URI by SendTo; the fake needs to know which intent is carrying one.
    harness = harness.replace("public class ComposerHandOver {",
                              "public class ComposerHandOver {\n  static String LINK_BODY = null;")
    harness = harness.replace('t.readIntent(Intent.link("+15550333", "here is the address"));',
                              'LINK_BODY = "here is the address";\n'
                              '    t.readIntent(Intent.link("+15550333", "here is the address"));\n'
                              '    LINK_BODY = null;')
    # The constants readIntent names are the activity's own.
    harness = harness.replace("public class ComposerHandOver {",
                              'public class ComposerHandOver {\n'
                              '  static final String EXTRA_THREAD = "t", EXTRA_ADDRESS = "a", EXTRA_THREADS = "ts";')

    src = tmp_path / "place/poster/app/sms/ComposerHandOver.java"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(harness, encoding="utf-8")
    out = tmp_path / "classes"
    c = subprocess.run(["javac", "-nowarn", "-d", str(out), str(src)],
                       capture_output=True, text=True, timeout=300)
    assert c.returncode == 0, c.stderr[-4000:]
    r = subprocess.run(["java", "-cp", str(out), "place.poster.app.sms.ComposerHandOver"],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "composer hand-over cases passed" in r.stdout
