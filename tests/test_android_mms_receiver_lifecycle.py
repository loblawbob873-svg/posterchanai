"""Execute the real receiver across Android's goAsync ownership transfer.

Android framework reference (goAsync clears mPendingResult; receiver getter then returns0):
https://android.googlesource.com/platform/frameworks/base/+/refs/heads/main/core/java/android/content/BroadcastReceiver.java
Only Android/provider/plugin boundaries are replaced; receiver and classifier are unchanged.
"""
import os
from pathlib import Path
import subprocess
import pytest

ROOT = Path(os.environ.get('PC_MMS_SOURCE_ROOT', Path(__file__).resolve().parents[1]))
STUBS = {
'android/content/BroadcastReceiver.java': '''package android.content;
public abstract class BroadcastReceiver {
 public static class PendingResult {
  private final int code; public volatile int finishes;
  public PendingResult(int value){code=value;}
  public final int getResultCode(){return code;}
  public void finish(){finishes++;}
 }
 private PendingResult pending;
 public final void setPendingResult(PendingResult value){pending=value;}
 public final PendingResult goAsync(){PendingResult value=pending;pending=null;return value;}
 public final int getResultCode(){return pending==null?0:pending.getResultCode();}
 public abstract void onReceive(Context context,Intent intent);
}''',
'android/content/Intent.java': '''package android.content;
public class Intent {
 private String action; private java.util.Map<String,Object> extras=new java.util.HashMap<>();
 public Intent(String a){action=a;} public String getAction(){return action;}
 public Intent setPackage(String p){return this;}
 public Intent putExtra(String k,Object v){extras.put(k,v);return this;}
 public String getStringExtra(String k){return (String)extras.get(k);}
 public int getIntExtra(String k,int d){return extras.containsKey(k)?(Integer)extras.get(k):d;}
}''',
'android/content/Context.java': '''package android.content;
public class Context {
 public final ContentResolver resolver=new ContentResolver(); public int broadcasts;
 public ContentResolver getContentResolver(){return resolver;}
 public String getPackageName(){return "place.poster.app";}
 public void sendBroadcast(Intent intent){broadcasts++;}
}''',
'android/content/ContentResolver.java': '''package android.content;
public class ContentResolver {
 public int box=4,updates;public boolean throwUpdate;
 public android.database.Cursor query(android.net.Uri uri,String[] p,String s,String[] a,String o){return new android.database.Cursor(box);}
 public int update(android.net.Uri uri,ContentValues v,String s,String[] a){
  if(throwUpdate)throw new SecurityException("provider denied");updates++;box=v.get("msg_box");return 1;
 }
}''',
'android/content/ContentValues.java': '''package android.content;
public class ContentValues extends java.util.HashMap<String,Integer>{}''',
'android/database/Cursor.java': '''package android.database;
public class Cursor implements AutoCloseable {
 private int box;public Cursor(int b){box=b;}public boolean moveToFirst(){return true;}
 public int getInt(int i){return box;}public void close(){}
}''',
'android/net/Uri.java': '''package android.net;
public class Uri {private String value;private Uri(String v){value=v;}
 public static Uri parse(String v){return new Uri(v);}public String toString(){return value;}
 public String getLastPathSegment(){return value.substring(value.lastIndexOf('/')+1);}
}''',
'android/provider/Telephony.java': '''package android.provider;
public class Telephony {public static class Mms {
 public static final String MESSAGE_BOX="msg_box";
 public static final int MESSAGE_BOX_SENT=2,MESSAGE_BOX_FAILED=5;
}}''',
'android/util/Log.java': '''package android.util;
public class Log {public static int w(String tag,String text,Throwable t){return 0;}}''',
'place/poster/app/sms/MmsFailures.java': '''package place.poster.app.sms;
public class MmsFailures {
 public static int clears,puts,code;
 public static void clear(android.content.Context c,long id){clears++;}
 public static void put(android.content.Context c,long id,int r,int h){puts++;code=r;}
 public static String reason(android.content.Context c,int r,int h){return "result:"+r;}
}''',
'place/poster/app/sms/MmsDraft.java': '''package place.poster.app.sms;
public class MmsDraft {
 public static final String SENT="sent",FAILED="failed",UNKNOWN="unknown";
 public static String state="",reason="";
 public static void state(android.content.Context c,String k,String s,String r){state=s;reason=r;}
}''',
'place/poster/app/sms/MmsFlight.java': '''package place.poster.app.sms;
public class MmsFlight {public static int releases;public static void release(android.content.Context c){releases++;}}''',
'place/poster/app/sms/SmsPlugin.java': '''package place.poster.app.sms;
public class SmsPlugin {public static int events,code;public static boolean ok;
 public static void onSendResult(String uri,boolean success,int result){events++;ok=success;code=result;}
}''',
'place/poster/app/sms/ReceiverProbe.java': '''package place.poster.app.sms;
import android.content.*;
public class ReceiverProbe {
 public static void main(String[] args)throws Exception {
  int code=Integer.parseInt(args[0]);String mode=args[1];
  Context ctx=new Context();ctx.resolver.throwUpdate=mode.equals("denied");
  MmsSendReceiver receiver=new MmsSendReceiver();
  BroadcastReceiver.PendingResult pending=new BroadcastReceiver.PendingResult(code);
  receiver.setPendingResult(pending);
  if(receiver.getResultCode()!=code)throw new AssertionError("fixture result setup");
  Intent intent=new Intent(MmsSendReceiver.ACTION_SENT).putExtra("draft_key","fixture-draft");
  if(!mode.equals("missing"))intent.putExtra("content_uri","content://mms/42");
  java.io.File file=java.io.File.createTempFile("mms-receiver-", ".pdu");
  intent.putExtra("file_path",file.getAbsolutePath());
  receiver.onReceive(ctx,intent);
  long deadline=System.nanoTime()+2_000_000_000L;
  while(pending.finishes==0 && System.nanoTime()<deadline)Thread.sleep(1);
  if(pending.finishes!=1 || MmsFlight.releases!=1)throw new AssertionError("finish/release not exactly once");
  if(receiver.getResultCode()!=0 || pending.getResultCode()!=code)throw new AssertionError("goAsync must detach original result");
  System.out.println(ctx.resolver.box+","+ctx.resolver.updates+","+MmsDraft.state+","+
   MmsFailures.clears+","+MmsFailures.puts+","+MmsFailures.code+","+SmsPlugin.events+","+
   SmsPlugin.ok+","+SmsPlugin.code+","+ctx.broadcasts+","+file.exists());
  file.delete();
 }
}''',
}


@pytest.fixture(scope='module')
def receiver_classes(tmp_path_factory):
    directory=tmp_path_factory.mktemp('mms-receiver-java')
    for name,text in STUBS.items():
        path=directory/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text)
    for name in ['MmsSendReceiver.java','MmsResult.java']:
        (directory/'place/poster/app/sms'/name).write_text((ROOT/'mobile/android/app/src/main/java/place/poster/app/sms'/name).read_text())
    result=subprocess.run(['javac','-d',str(directory),*[str(p) for p in directory.rglob('*.java')]],capture_output=True,text=True,timeout=20)
    assert result.returncode==0,result.stderr
    return directory


@pytest.mark.parametrize('code,expected',[
    (-1,'2,1,sent,1,0,0,1,true,-1,1,false'),
    (1,'5,1,failed,0,1,1,1,false,1,1,false'),
    (2,'5,1,failed,0,1,2,1,false,2,1,false'),
    (8,'5,1,failed,0,1,8,1,false,8,1,false'),
    (0,'4,0,unknown,0,1,0,0,false,0,1,false'),
])
def test_real_mms_receiver_keeps_carrier_result_after_go_async(receiver_classes,code,expected):
    result=subprocess.run(['java','-cp',str(receiver_classes),'place.poster.app.sms.ReceiverProbe',str(code),'normal'],capture_output=True,text=True,timeout=5)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()==expected


@pytest.mark.parametrize('mode',['missing','denied'])
def test_real_mms_receiver_finishes_and_releases_on_exception(receiver_classes,mode):
    result=subprocess.run(['java','-cp',str(receiver_classes),'place.poster.app.sms.ReceiverProbe','-1',mode],capture_output=True,text=True,timeout=5)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='4,0,,0,0,0,0,false,0,0,true'
