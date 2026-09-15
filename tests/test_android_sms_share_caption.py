"""Run real readIntent/import callbacks: a cancelled photo share cannot change the old caption."""
from pathlib import Path
import subprocess
from tests.test_android_composer_belongs_to_the_conversation import _method

ROOT = Path(__file__).resolve().parents[1]


def test_shared_caption_commits_only_with_successful_staging(tmp_path):
    source = (ROOT/'mobile/android/app/src/main/java/place/poster/app/sms/ThreadActivity.java').read_text()
    methods = '\n'.join(_method(source, start) for start in ['    private void readIntent(Intent i)',
                                                            '    private void importSharedAttachment(Intent intent)'])
    harness = r'''
class Probe {
 static final String EXTRA_THREAD="t", EXTRA_ADDRESS="a", EXTRA_THREADS="ts";
 String address="recipient";long threadId;long[] threadIds;Box input=new Box();Object attachment;
 Intent current;boolean busy,metadataFailure;int prepared;Runnable ready;
 void handOverComposer(String a){input.setText("");} boolean attachmentBusy(){return busy;}
 Intent getIntent(){return current;}void say(String x){}String getString(int x){return "unreadable";}
 void prepareAttachment(Uri uri){prepareAttachment(uri,null);}
 void prepareAttachment(Uri uri,Runnable callback){if(metadataFailure)throw new SecurityException();prepared++;ready=callback;}
 void deliver(Intent i){current=i;readIntent(i);}
 static void check(boolean yes,String why){if(!yes)throw new AssertionError(why);}
 METHODS
 public static void main(String[] args){
  for(String failure:new String[]{"cancel","busy","metadata","copy"}){
   Probe p=new Probe();MmsDraft.present=true;p.busy=failure.equals("busy");p.metadataFailure=failure.equals("metadata");
   p.deliver(new Intent("new caption"));
   check(p.input.value.isEmpty(),failure+": incoming caption changed old draft before acceptance");
   if(failure.equals("cancel"))Dialog.cancel.run();
   else if(!p.busy)Dialog.accept.run();
   check(p.input.value.isEmpty(),failure+": failed share left a new caption on old attachment");
  }
  Probe p=new Probe();MmsDraft.present=true;p.deliver(new Intent("accepted caption"));Dialog.accept.run();
  check(p.prepared==1 && p.input.value.isEmpty(),"caption appeared before file copy succeeded");
  check(p.ready!=null,"staging completion callback missing");p.ready.run();
  check(p.input.value.equals("accepted caption"),"successful share lost caption");
  Probe typed=new Probe();typed.deliver(new Intent("shared"));Dialog.accept.run();typed.input.setText("typed during copy");typed.ready.run();
  check(typed.input.value.equals("typed during copy"),"completion overwrote newer typing");
  Probe stale=new Probe();stale.deliver(new Intent("stale caption"));Dialog.accept.run();stale.current=new Intent("new intent");stale.ready.run();
  check(stale.input.value.isEmpty(),"stale completion changed new intent's composer");
 }
}
class Box {String value="";String getText(){return value;}void setText(String s){value=s;}}
class Uri {}
class Intent {
 static final String EXTRA_TEXT="text";String caption;boolean consumed;
 Intent(String s){caption=s;}long getLongExtra(String s,long d){return d;}
 String getStringExtra(String s){return "recipient";}String getData(){return null;}
 CharSequence getCharSequenceExtra(String s){return caption;}long[] getLongArrayExtra(String s){return null;}
}
class SmsShare {static Uri stream(Intent i){return i==null||i.consumed?null:new Uri();}static void consumed(Intent i){i.consumed=true;}}
class SendTo {static boolean isMessageUri(String s){return false;}static String numberFrom(String s){return "";}static String bodyFrom(String s){return "";}}
class SmsStore {static long threadIdFor(Object c,String a){return 1;}static long[] idsFor(Object c,String a,long t){return new long[]{t};}}
class MmsDraft {static boolean present;static class Value{}static Value load(Object c,String a){return present?new Value():null;}static void setText(Object c,String a,String s){}}
class R {static class string {static final int sms_attachment_bad=1;}}
class Dialog {static Runnable accept,cancel;}
class AlertDialog {interface Click{void call(Object d,int w);}interface Cancel{void call(Object d);}
 static class Builder {
 Builder(Object c){}Builder setTitle(String s){return this;}Builder setMessage(String s){return this;}
 Builder setPositiveButton(int id,Click c){Dialog.accept=()->c.call(null,0);return this;}
 Builder setNegativeButton(int id,Click c){Dialog.cancel=()->c.call(null,0);return this;}
 Builder setOnCancelListener(Cancel c){return this;}void show(){}
 }}
'''.replace('METHODS', methods)
    (tmp_path/'Probe.java').write_text(harness)
    android = tmp_path/'android/R.java';android.parent.mkdir();android.write_text('package android;public class R {public static class string {public static final int ok=1,cancel=2;}}')
    built=subprocess.run(['javac','-d',str(tmp_path),str(android),str(tmp_path/'Probe.java')],capture_output=True,text=True,timeout=20)
    assert built.returncode==0,built.stderr
    run=subprocess.run(['java','-cp',str(tmp_path),'Probe'],capture_output=True,text=True,timeout=5)
    assert run.returncode==0,run.stdout+run.stderr
