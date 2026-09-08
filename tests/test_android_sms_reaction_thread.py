"""Execute the native history adapter: no Android/provider mocks hide identity decisions."""
from pathlib import Path
import subprocess
ROOT=Path(__file__).resolve().parents[1]

def test_native_reaction_history_and_outgoing_guards(tmp_path):
    harness=r'''
package place.poster.app.sms;
import java.util.*;
public class Probe {
 static void check(boolean b,String s){if(!b)throw new AssertionError(s);}
 static SmsMsg row(long id,String body,int type){SmsMsg m=new SmsMsg();m.id=id;m.threadId=9;m.address="+15551234567";m.date=id*1000;m.body=body;m.type=type;return m;}
 static SmsReactionThread history(SmsMsg...rows){return new SmsReactionThread(Arrays.asList(rows),true,9,"+15551234567");}
 public static void main(String[]args){
  SmsMsg target=row(1,"Exact\ntext “quoted”",1);
  for(String kind:new String[]{"heart","like","dislike","laugh","emphasize","question"}){
   for(boolean remove:new boolean[]{false,true}){
    SmsReactions.Parsed p=SmsReactions.parse(SmsReactions.format(kind,remove,target.body));
    check(p.text.equals(target.body)&&p.kind.equals(kind)&&p.operation.equals(remove?"remove":"add"),"wire roundtrip");
   }
  }
  try{SmsReactions.format("evil",false,"text");throw new AssertionError("invalid kind");}catch(IllegalArgumentException expected){}
  SmsMsg added=row(2,SmsReactions.format("heart",false,target.body),2);
  SmsReactionThread h=history(target,added);
  check(h.visible.size()==1&&h.raw.size()==2,"collapse presentation only");
  check(h.own(target).kind.equals("heart")&&h.canReact(target),"own reaction editable");
  SmsMsg changed=row(3,SmsReactions.format("like",false,target.body),2);
  check(history(target,added,changed).own(target).kind.equals("like"),"change");
  SmsMsg removed=row(4,SmsReactions.format("like",true,target.body),2);
  check(history(target,added,changed,removed).own(target)==null,"remove");
  SmsMsg pending=row(3,changed.body,4);
  check(!history(target,pending).canReact(target),"pending prevents another action");
  pending.type=6;check(!history(target,pending).canReact(target),"queued prevents another action");
  pending.type=5;check(history(target,pending).canReact(target),"definite failure can retry");
  check(!history(target,row(5,target.body,1)).canReact(target),"duplicate exact text is ambiguous");
  check(!new SmsReactionThread(Arrays.asList(target,added),false,9,target.address).canReact(target),"incomplete history");
  check(new SmsReactionThread(Arrays.asList(target,added),false,9,target.address).visible.size()==2,"incomplete stays raw");
  SmsMsg edited=row(1,"changed",1);check(!history(edited).canReact(target),"stale UI target");
  SmsMsg alien=row(3,"other",1);alien.threadId=10;check(!history(target,alien).complete,"strict thread");
  alien.threadId=9;alien.address="+445551234567";check(!history(target,alien).complete,"no suffix phone matching");
  alien.address=target.address;alien.people=2;check(!history(target,alien).complete,"group");
  alien.people=1;alien.mms=true;check(!history(target,alien).complete,"media identity uncertain");
  SmsMsg group=row(8,"text",1);group.address="+15551234567;+15557654321";
  check(!new SmsReactionThread(Arrays.asList(group),true,9,group.address).complete,"recipient list is not one number");
  SmsMsg own=row(1,"sent",2), incoming=row(2,"Liked “sent”",1);
  check(history(own,incoming).visible.size()==1,"incoming reaction on own text");
  check(!history(own,incoming).canReact(own),"no reaction to self");
 }
}
'''
    path=tmp_path/'Probe.java';path.write_text(harness)
    base=ROOT/'mobile/android/app/src/main/java/place/poster/app/sms'
    files=[base/(name+'.java') for name in ['SmsReactionThread','SmsReactions','SmsMsg','SmsPart','SmsKeys']]
    run=subprocess.run(['javac','-encoding','UTF-8','-d',str(tmp_path),*map(str,files),str(path)],capture_output=True,text=True,timeout=30)
    assert run.returncode==0,run.stderr
    run=subprocess.run(['java','-cp',str(tmp_path),'place.poster.app.sms.Probe'],capture_output=True,text=True,timeout=10)
    assert run.returncode==0,run.stderr


def test_actual_native_reaction_send_handler_rechecks_and_does_not_repeat(tmp_path):
    base=ROOT/'mobile/android/app/src/main/java/place/poster/app/sms'
    source=(base/'ThreadActivity.java').read_text()
    method=source[source.index('    private void sendReaction('):source.index('    private void messageMenu(')]
    harness=r'''
package place.poster.app.sms;
import java.util.*;import java.util.concurrent.*;
public class Probe {
 static final Set<String> reactionSends=Collections.synchronizedSet(new HashSet<String>());
 long threadId=9;String address="+15551234567",input="unsent draft";
 static List<SmsMsg> rows;static int sends;static boolean role=true;
 static CountDownLatch reading,release,done;static String wire;
 static SmsMsg row(long id,String text,int type){SmsMsg m=new SmsMsg();m.id=id;m.threadId=9;m.address="+15551234567";m.body=text;m.date=id*1000;m.type=type;return m;}
 static void check(boolean b,String message){if(!b)throw new AssertionError(message);}
 SmsReactionThread reactionHistory(long id,String peer){reading.countDown();try{release.await();}catch(Exception e){throw new RuntimeException(e);}return new SmsReactionThread(rows,true,id,peer);}
 class Main {void post(Runnable action){action.run();done.countDown();}} Main main=new Main();
 void reload(){}void say(String error){}String getString(int id){return "error";}
''' + method + r'''
 static void reset(SmsMsg... current){rows=new ArrayList<>(Arrays.asList(current));sends=0;wire=null;reading=new CountDownLatch(1);release=new CountDownLatch(1);done=new CountDownLatch(1);reactionSends.clear();role=true;}
 static void finish()throws Exception{release.countDown();check(done.await(3,TimeUnit.SECONDS),"handler completion");}
 public static void main(String[]args)throws Exception{
  SmsMsg original=row(1,"target",1);
  reset(original);Probe p=new Probe();p.sendReaction(original,"like",false);check(reading.await(3,TimeUnit.SECONDS),"read started");p.sendReaction(original,"like",false);finish();
  check(sends==1&&wire.equals("Liked “target”"),"double tap must send once");check(p.input.equals("unsent draft"),"composer unchanged");
  done=new CountDownLatch(1);new Probe().sendReaction(original,"like",false);finish();check(sends==1,"new screen pending provider row blocks resend");
  reset(row(1,"edited",1));new Probe().sendReaction(original,"like",false);finish();check(sends==0,"stale edited target");
  reset(original);p=new Probe();p.sendReaction(original,"like",false);reading.await();p.threadId=10;finish();check(sends==0,"conversation switched during read");
  reset(original,row(2,"Liked “target”",2));new Probe().sendReaction(original,"like",false);finish();check(sends==0,"already own kind no repeat");
  reset(original,row(2,"Liked “target”",2));new Probe().sendReaction(original,"heart",false);finish();check(sends==1&&wire.equals("Loved “target”"),"change own reaction");
  reset(original,row(2,"Liked “target”",2));new Probe().sendReaction(original,"like",true);finish();check(sends==1&&wire.equals("Removed a like from “target”"),"remove own reaction");
  reset(original,row(2,"Loved “target”",2));new Probe().sendReaction(original,"like",true);finish();check(sends==0,"stale remove must not remove changed reaction");
  reset(original);role=false;new Probe().sendReaction(original,"like",false);check(sends==0&&reactionSends.isEmpty(),"no role no submission");
 }
}
class HasRole {static boolean sms(Object ctx){return Probe.role;}}
class SmsSender {static class Result{boolean ok=true;String error="";}static Result sendStored(Object ctx,String peer,String body,long id){Probe.sends++;Probe.wire=body;Probe.rows.add(Probe.row(100,body,4));return new Result();}}
class R {static class string {static int sms_reaction_failed=1;}}
'''
    path=tmp_path/'Probe.java';path.write_text(harness)
    files=[base/(name+'.java') for name in ['SmsReactionThread','SmsReactions','SmsMsg','SmsPart','SmsKeys']]
    run=subprocess.run(['javac','-encoding','UTF-8','-d',str(tmp_path),*map(str,files),str(path)],capture_output=True,text=True,timeout=30)
    assert run.returncode==0,run.stderr
    run=subprocess.run(['java','-cp',str(tmp_path),'place.poster.app.sms.Probe'],capture_output=True,text=True,timeout=15)
    assert run.returncode==0,run.stderr


def test_reaction_sender_requires_durable_row_before_carrier(tmp_path):
    source=(ROOT/'mobile/android/app/src/main/java/place/poster/app/sms/SmsSender.java').read_text()
    method=source[source.index('    public static Result send(Context ctx, String address, String body, long threadId)'):source.index('    private static SmsManager manager(')]
    harness=r'''
import java.util.*;
public class Probe {
 static boolean role,store;static int calls,writes;static final String TAG="test",ACTION_SENT="sent",ACTION_DELIVERED="delivered";
 static class Result {boolean ok,stored;String error;Object row;long sentAt;int parts;}
 static class Context {}static class PendingIntent {}
 static class Telephony {static class Sms {static final int MESSAGE_TYPE_OUTBOX=4,MESSAGE_TYPE_FAILED=5;}}
 static class Log {static void w(String tag,String message,Throwable t){}}
 static class HasRole {static boolean sms(Context c){return role;}}
 static class SmsStore {static Object storeSent(Context c,String a,String b,long d,int t,long id){writes++;return store?new Object():null;}static void setType(Context c,Object row,int type){}}
 static class SmsManager {ArrayList<String> divideMessage(String body){return new ArrayList<>(Arrays.asList(body));}void sendMultipartTextMessage(String a,Object b,ArrayList<String> c,ArrayList<PendingIntent> d,ArrayList<PendingIntent> e){calls++;}}
 static SmsManager manager(Context c){return new SmsManager();}
 static PendingIntent signal(Context c,String action,Object row,int part){return new PendingIntent();}
''' + method + r'''
 static void check(boolean b,String reason){if(!b)throw new AssertionError(reason);}
 public static void main(String[]args){
  Context c=new Context();role=true;store=false;
  Result r=sendStored(c,"peer","Liked “text”",1);check(!r.ok&&!r.stored&&calls==0,"failed insert must never reach carrier");
  role=false;r=sendStored(c,"peer","Liked “text”",1);check(!r.ok&&calls==0,"role loss must never reach carrier");
  role=true;store=true;r=sendStored(c,"peer","Liked “text”",1);check(r.ok&&r.stored&&calls==1,"stored reaction sends");
  role=false;store=false;r=send(c,"peer","ordinary SMS",1);check(r.ok&&calls==2,"existing ordinary SMS behavior retained");
 }
}
'''
    path=tmp_path/'Probe.java';path.write_text(harness)
    run=subprocess.run(['javac','-encoding','UTF-8','-d',str(tmp_path),str(path)],capture_output=True,text=True,timeout=30)
    assert run.returncode==0,run.stderr
    run=subprocess.run(['java','-cp',str(tmp_path),'Probe'],capture_output=True,text=True,timeout=10)
    assert run.returncode==0,run.stderr
