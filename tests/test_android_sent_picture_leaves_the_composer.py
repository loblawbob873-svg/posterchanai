"""A picture message that has been sent leaves the composer, so the next message is just the next message.

Reported: "tried to send picture on android and the media stays in the text message and messes up the
message you are trying to send next, sending an attachment should clear the text message so you can
send a follow up". The native Texts screen kept the picture on the composer as "Sending…" then
"Sent" after the carrier took it; send() refuses any draft that is not READY or FAILED, so the next
text typed there did nothing but say "sent", and attachmentBusy() refused new attachments while it
waited.

Now: once MmsSender accepts the message the draft is removed, and the carrier's later verdict only
lands on a draft that is still SENDING (never on a newer picture picked afterwards). MmsDraft is RUN
here under javac with a stub Context.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SMS = ROOT / "mobile/android/app/src/main/java/place/poster/app/sms"

STUBS = {
    "android/content/SharedPreferences.java": """package android.content;
public class SharedPreferences {
 final java.util.HashMap<String,String> map=new java.util.HashMap<>();
 public String getString(String k,String d){return map.getOrDefault(k,d);}
 public Editor edit(){return new Editor();}
 public class Editor {
  final java.util.Map<String,String> ch=new java.util.HashMap<>();
  public Editor putString(String k,String v){ch.put(k,v);return this;}
  public Editor remove(String k){ch.put(k,null);return this;}
  public boolean commit(){for(java.util.Map.Entry<String,String> e:ch.entrySet())
    if(e.getValue()==null)map.remove(e.getKey());else map.put(e.getKey(),e.getValue());return true;}
  public void apply(){commit();}
 }
}""",
    "android/content/Context.java": """package android.content;
public class Context {
 public static final int MODE_PRIVATE=0;final java.io.File root;final SharedPreferences prefs=new SharedPreferences();
 public Context(java.io.File root){this.root=root;}
 public java.io.File getFilesDir(){return root;}
 public SharedPreferences getSharedPreferences(String n,int m){return prefs;}
}""",
    "place/poster/app/sms/Probe.java": """package place.poster.app.sms;
public class Probe {
 static void check(boolean b,String why){if(!b)throw new AssertionError(why);}
 public static void main(String[] a)throws Exception {
  android.content.Context ctx=new android.content.Context(new java.io.File(a[0]));
  String who="+15550100";String key=MmsDraft.key(who);
  // A verdict for a picture that already left the composer writes nothing.
  MmsDraft.result(ctx,key,MmsDraft.SENT,"");
  check(MmsDraft.load(ctx,who)==null,"a verdict created a draft");
  // A newer picture picked while the carrier worked is READY and stays READY.
  MmsDraft.save(ctx,who,new byte[]{1,2,3},"image/jpeg","next.jpg");
  MmsDraft.result(ctx,key,MmsDraft.SENT,"");
  check(MmsDraft.READY.equals(MmsDraft.load(ctx,who).state),"the old verdict landed on the new picture");
  MmsDraft.result(ctx,key,MmsDraft.FAILED,"boom");
  check(MmsDraft.READY.equals(MmsDraft.load(ctx,who).state),"a failure landed on the new picture");
  // A draft genuinely waiting (a private-link upload) still hears its verdict.
  MmsDraft.state(ctx,key,MmsDraft.SENDING,"");
  MmsDraft.result(ctx,key,MmsDraft.FAILED,"no signal");
  check(MmsDraft.FAILED.equals(MmsDraft.load(ctx,who).state),"a waiting draft lost its verdict");
  System.out.println("ok");
 }
}""",
}


@pytest.mark.skipif(not shutil.which("javac") or not shutil.which("java"), reason="no JDK")
def test_the_carrier_verdict_only_lands_on_the_draft_still_waiting_for_it(tmp_path):
    for name, text in STUBS.items():
        p = tmp_path / "src" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    shutil.copy(SMS / "MmsDraft.java", tmp_path / "src/place/poster/app/sms/MmsDraft.java")
    out = tmp_path / "out"
    r = subprocess.run(["javac", "-nowarn", "-d", str(out), *map(str, (tmp_path / "src").rglob("*.java"))],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    data = tmp_path / "data"
    data.mkdir()
    r = subprocess.run(["java", "-cp", str(out), "place.poster.app.sms.Probe", str(data)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "ok", r.stdout + r.stderr


def _between(src, start, end):
    return src[src.index(start):src.index(end, src.index(start))]


def test_an_accepted_picture_message_is_removed_from_the_composer():
    thread = (SMS / "ThreadActivity.java").read_text(encoding="utf-8")
    send = _between(thread, "private void sendMms(String body)", "private void call()")
    accepted = send[send.index("SmsSender.Result result = MmsSender.send("):]
    accepted = accepted[:accepted.index('say(getString(R.string.sms_mms_sent));')]
    assert "MmsDraft.remove(this, address)" in accepted
    assert "attachmentDraft = null;" in accepted
    assert 'input.setText("")' in accepted
    assert "MmsDraft.SENDING" not in accepted, "the sent picture is parked on the composer again"
    receiver = (SMS / "MmsSendReceiver.java").read_text(encoding="utf-8")
    assert "MmsDraft.result(ctx, draftKey," in receiver
    assert "MmsDraft.state(ctx, draftKey," not in receiver


def test_a_finished_draft_left_by_an_older_build_does_not_ride_the_next_message():
    thread = (SMS / "ThreadActivity.java").read_text(encoding="utf-8")
    restore = _between(thread, "private void restoreAttachmentDraft()", "private void clearAttachmentDraft()")
    assert "MmsDraft.SENT.equals(attachmentDraft.state)" in restore
    assert "MmsDraft.UNKNOWN.equals(attachmentDraft.state)" in restore
    assert "MmsDraft.remove(this, address)" in restore
