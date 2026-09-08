"""Run the real bounded Java reply queue; transport rejection must not lose prepared bytes."""
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def test_reconnect_reuses_bytes_rejects_stale_pairings_and_bounds_retention(tmp_path):
    source=ROOT/'mobile/android/app/src/main/java/place/poster/app/signer/SignerReplyQueue.java'
    harness=tmp_path/'ReplyQueueCheck.java'
    harness.write_text('''package place.poster.app.signer;
public class ReplyQueueCheck {
  static void check(boolean value,String reason){if(!value)throw new AssertionError(reason);}
  public static void main(String[] args){
    SignerReplyQueue<Object> q=new SignerReplyQueue<>(); Object session=new Object();
    String signed=new String("already-signed-correlated-reply");
    check(q.add(session,signed,0),"queue prepared reply");
    check(q.flush(1,s->s==session,(s,wire)->false)==0,"send false is not answered");
    check(q.size()==1,"rejected transport retains reply");
    int[] sends={0};
    check(q.flush(2,s->s==session,(s,wire)->{check(wire==signed,"must reuse exact prepared bytes");sends[0]++;return true;})==1,"replacement socket accepted");
    check(q.flush(3,s->true,(s,wire)->{sends[0]++;return true;})==0 && sends[0]==1,"no duplicate delivery after acceptance");
    q.add(session,signed,4);
    check(q.flush(5,s->false,(s,wire)->{throw new AssertionError("revoked pairing sent");})==0 && q.size()==0,"revoke cancels queued reply");
    q.add(session,signed,6);
    check(q.flush(120006,s->true,(s,wire)->{throw new AssertionError("expired reply sent");})==0,"expired reply refused");
    for(int i=0;i<64;i++)check(q.add(session,signed,130000),"bounded capacity");
    check(!q.add(session,signed,130000),"overflow refused");
    check(q.add(session,signed,250000),"expired capacity reclaimed");
    q.clear();check(q.size()==0,"shutdown clears queue");
    System.out.println("reply queue transport/reconnect/revoke/expiry/capacity PASS");
  }
}''')
    subprocess.run(['javac','-d',str(tmp_path),str(source),str(harness)],check=True,capture_output=True,text=True)
    done=subprocess.run(['java','-cp',str(tmp_path),'place.poster.app.signer.ReplyQueueCheck'],check=True,capture_output=True,text=True)
    assert 'PASS' in done.stdout
