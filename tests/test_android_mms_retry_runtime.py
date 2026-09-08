"""Run shipped native retry policy/handler against a durable provider boundary; no carrier sends."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SMS = ROOT / 'mobile/android/app/src/main/java/place/poster/app/sms'


def test_failed_mms_retry_rechecks_and_claims_provider_row(tmp_path):
    thread = (SMS / 'ThreadActivity.java').read_text()
    methods = thread[thread.index('    private static boolean retryableMms('):
                     thread.index('    private void deleteMessage(')]
    failure = (SMS / 'MmsFailures.java').read_text()
    unknown = failure[failure.index('    static boolean indeterminate('):
                      failure.index('    /* SmsManager', failure.index('    static boolean indeterminate('))]
    files = {
        'android/net/Uri.java': 'package android.net; public class Uri {public String value;public static Uri parse(String s){Uri u=new Uri();u.value=s;return u;}}',
        'android/content/ContentValues.java': 'package android.content; public class ContentValues extends java.util.HashMap<String,Integer>{}',
        'android/provider/Telephony.java': 'package android.provider; public class Telephony {public static class Mms {public static final int MESSAGE_BOX_FAILED=5,MESSAGE_BOX_OUTBOX=4;public static final String MESSAGE_BOX="msg_box";}}',
        'place/poster/app/sms/MmsFailures.java': 'package place.poster.app.sms; class MmsFailures {\n'+unknown+'\n}',
    }
    for name in ['SmsMsg.java', 'SmsPart.java', 'SmsKeys.java']:
        files['place/poster/app/sms/'+name] = (SMS / name).read_text()
    files['place/poster/app/sms/RetryHarness.java'] = r'''
package place.poster.app.sms;
import android.net.Uri;
public class RetryHarness {
 static SmsMsg row; static int sends,deletes,archives,claims; static boolean accepted,deleteOk,claimOk,rollbackOk,changeDuringRead;
 static SmsMsg msg(int type,String error){SmsMsg m=new SmsMsg();m.id=7;m.date=123000;m.address="fixture";m.body="caption";m.mms=true;m.type=type;m.error=error;SmsPart p=new SmsPart();p.id=8;p.ct="image/jpeg";p.name="fixture.jpg";m.parts.add(p);return m;}
 static void reset(){row=msg(5,"");sends=deletes=archives=claims=0;accepted=deleteOk=claimOk=rollbackOk=true;changeDuringRead=false;}
 static void check(boolean b,String why){if(!b)throw new AssertionError(why);}
 static class Resolver {
  int update(Uri u,android.content.ContentValues v,String where,String[] args){
   check(u.value.equals("content://mms/7")&&where.equals("msg_box=?"),"must conditionally update exact row");
   int from=Integer.parseInt(args[0]);if(row==null||row.type!=from)return 0;
   if(from==5){claims++;if(!claimOk)return 0;}else if(!rollbackOk)return 0;
   row.type=v.get("msg_box");return 1;
  }
 }
 Resolver getContentResolver(){return new Resolver();} void reload(){} void say(String s){} String getString(int id){return "message";}
''' + methods + r'''
 public static void main(String[] args){
  for(int type:new int[]{1,2,3,4,6})for(String error:new String[]{"","delivery unknown","carrier send status is pending"}){
   reset();row=msg(type,error);check(!retryableMms(row),"policy must reject pending/nonfailed rows");new RetryHarness().retryMms(row);check(sends==0&&claims==0&&deletes==0,"nonfailed must never retry");
  }
  for(String error:new String[]{"delivery unknown","carrier send status is pending — may have sent"}){
   reset();row.error=error;new RetryHarness().retryMms(row);check(sends==0,"unknown failed row must not retry");
  }
  reset();SmsMsg stale=msg(5,"");row.type=4;new RetryHarness().retryMms(stale);check(sends==0,"stale failed UI / live pending");
  reset();row.error="delivery unknown";new RetryHarness().retryMms(stale);check(sends==0,"fresh unknown reason");
  reset();row=null;new RetryHarness().retryMms(stale);check(sends==0,"deleted row");
  reset();row.body="different message";new RetryHarness().retryMms(stale);check(sends==0,"row identity changed");
  reset();changeDuringRead=true;new RetryHarness().retryMms(stale);check(sends==0,"row changed while attachment read");
  reset();claimOk=false;new RetryHarness().retryMms(stale);check(sends==0,"failed durable claim");
  reset();deleteOk=false;new RetryHarness().retryMms(stale);check(sends==1&&row.type==4&&archives==0,"accepted retry persists pending when deletion fails");
  new RetryHarness().retryMms(stale);check(sends==1,"fresh activity cannot repeat accepted retry");
  reset();new RetryHarness().retryMms(stale);check(sends==1&&row==null&&archives==1,"accepted retry retires only original");
  reset();accepted=false;new RetryHarness().retryMms(stale);check(sends==1&&row.type==5&&deletes==0,"sync refusal preserves failed original");
  accepted=true;new RetryHarness().retryMms(stale);check(sends==2&&archives==1,"definitive failed retry remains usable");
  reset();accepted=false;rollbackOk=false;new RetryHarness().retryMms(stale);check(row.type==4,"failed rollback remains conservative");
  new RetryHarness().retryMms(stale);check(sends==1,"failed rollback must not duplicate");
  System.out.println("retry policy and durable provider transition cases passed");
 }
}
class MmsStore {
 static SmsMsg one(Object ctx,Uri uri){return RetryHarness.row;}
 static byte[] partBytes(Object ctx,long id,int max){if(RetryHarness.changeDuringRead)RetryHarness.row.type=4;return new byte[]{1};}
 static int delete(Object ctx,long[] ids){RetryHarness.deletes++;if(!RetryHarness.deleteOk)return 0;RetryHarness.row=null;return 1;}
}
class SmsSender {static class Result {boolean ok;String error="refused";}}
class MmsSender {static SmsSender.Result send(Object c,String a,String b,byte[] bytes,String ct,String name){
 RetryHarness.check(RetryHarness.row.type==4,"claim must precede carrier submission");RetryHarness.sends++;
 SmsSender.Result r=new SmsSender.Result();r.ok=RetryHarness.accepted;return r;}}
class SignerRelayService {static void archiveDelete(Object c,String id){RetryHarness.archives++;}}
class R {static class string {static int sms_attachment_bad=1,sms_failed=2,sms_retrying=3;}}
'''
    for name, source in files.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(source)
    compiled = subprocess.run(['javac', '-d', str(tmp_path/'classes')]+[str(tmp_path/n) for n in files],
                              text=True, capture_output=True, timeout=30)
    assert compiled.returncode == 0, compiled.stderr
    run = subprocess.run(['java', '-cp', str(tmp_path/'classes'), 'place.poster.app.sms.RetryHarness'],
                         text=True, capture_output=True, timeout=20)
    assert run.returncode == 0, run.stdout + run.stderr


def test_attachment_send_reloads_durable_state_before_retry(tmp_path):
    thread = (SMS/'ThreadActivity.java').read_text()
    start = thread.index('    private void send() {')
    method = thread[start:thread.index('    /** Pick through', start)]
    source = r'''
class DraftHarness {
 static class Input {String value="caption";String getText(){return value;}void setText(String s){value=s;}}
 Input input=new Input();String address="fixture";long threadId=1;
 Object attachment=null,capturedAttachment=null;MmsDraft.Value attachmentDraft,fresh;
 int pictureSends=0;boolean attachmentBusy(){return false;}
 void restoreAttachmentDraft(){attachmentDraft=fresh;}
 void say(String s){}String getString(int id){return "message";}void updateCount(){}void reload(){}
 void sendMms(String body){pictureSends++;}
''' + method + r'''
 static void check(boolean yes,String why){if(!yes)throw new AssertionError(why);}
 public static void main(String[] args){
  for(String state:new String[]{"sending","sent","delivery unknown"}){
   DraftHarness h=new DraftHarness();h.attachmentDraft=new MmsDraft.Value("ready","");h.fresh=new MmsDraft.Value(state,"");
   h.send();check(h.pictureSends==0&&SmsSender.calls==0,"stale ready draft must not resend "+state);
  }
  DraftHarness h=new DraftHarness();h.attachmentDraft=new MmsDraft.Value("ready","");h.fresh=null;h.send();
  check(h.pictureSends==0&&SmsSender.calls==0,"retired attachment must not become duplicate caption SMS");
  h=new DraftHarness();h.attachmentDraft=new MmsDraft.Value("failed","");h.fresh=new MmsDraft.Value("failed","delivery unknown");h.send();check(h.pictureSends==0,"unknown failed legacydraft must not resend");
  for(String state:new String[]{"ready","failed"}){h=new DraftHarness();h.attachmentDraft=new MmsDraft.Value("sending","");h.fresh=new MmsDraft.Value(state,"");h.send();check(h.pictureSends==1,"definitive failed/ready must stay sendable");}
  h=new DraftHarness();h.send();check(SmsSender.calls==1,"normal SMS still sends");
 }
}
class MmsDraft {static String READY="ready",FAILED="failed";static class Value {String state,error;Value(String s,String e){state=s;error=e;}}}
class MmsFailures {static boolean indeterminate(String error){return error.startsWith("delivery unknown");}}
class SmsSender {static int calls;static class Result {boolean ok=true;String error="";}static Result send(Object c,String a,String b,long t){calls++;return new Result();}}
class R {static class string {static int sms_not_default=1,sms_failed=2;}}
'''
    path=tmp_path/'DraftHarness.java';path.write_text(source)
    r=subprocess.run(['javac',str(path)],text=True,capture_output=True,timeout=30)
    assert r.returncode==0,r.stderr
    r=subprocess.run(['java','-cp',str(tmp_path),'DraftHarness'],text=True,capture_output=True,timeout=20)
    assert r.returncode==0,r.stdout+r.stderr
