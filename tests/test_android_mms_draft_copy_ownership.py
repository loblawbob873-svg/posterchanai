"""Real concurrent draft copies: newer owners win without waiting for slow providers."""
from tests.test_android_composer_belongs_to_the_conversation import _method
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_blocked_copy_cannot_overwrite_newer_bytes_text_or_deletion(tmp_path):
    sources = {
        'android/content/SharedPreferences.java': '''package android.content;
public class SharedPreferences {
 final java.util.concurrent.ConcurrentHashMap<String,String> map=new java.util.concurrent.ConcurrentHashMap<>();
 public String getString(String k,String d){return map.getOrDefault(k,d);}
 public Editor edit(){return new Editor();}
 public class Editor {
  final java.util.Map<String,String> changes=new java.util.HashMap<>();
  public Editor putString(String k,String v){changes.put(k,v);return this;}
  public Editor remove(String k){changes.put(k,null);return this;}
  public boolean commit(){for(java.util.Map.Entry<String,String> e:changes.entrySet())
    if(e.getValue()==null)map.remove(e.getKey());else map.put(e.getKey(),e.getValue());return true;}
  public void apply(){commit();}
 }
}''',
        'android/content/Context.java': '''package android.content;
public class Context {
 public static final int MODE_PRIVATE=0;final java.io.File root;
 final SharedPreferences prefs=new SharedPreferences();
 public Context(java.io.File root){this.root=root;}
 public java.io.File getFilesDir(){return root;}
 public SharedPreferences getSharedPreferences(String n,int mode){return prefs;}
}''',
        'place/poster/app/sms/Probe.java': r'''package place.poster.app.sms;
import java.io.*;import java.nio.file.*;import java.util.concurrent.*;import java.util.concurrent.atomic.*;
public class Probe {
 static class Blocked extends InputStream {
  final CountDownLatch entered=new CountDownLatch(1),release=new CountDownLatch(1);boolean done;
  public int read()throws IOException{throw new AssertionError("bulk copy expected");}
  public int read(byte[] b)throws IOException{
   if(done)return -1;entered.countDown();
   try{if(!release.await(3,TimeUnit.SECONDS))throw new IOException("reader globally serialized");}
   catch(InterruptedException e){throw new IOException(e);}done=true;b[0]=11;return 1;
  }
 }
 static void check(boolean b,String why){if(!b)throw new AssertionError(why);}
 static void expect(android.content.Context ctx,String who,String name,int value)throws Exception {
  MmsDraft.Value draft=MmsDraft.load(ctx,who);check(draft!=null&&draft.name.equals(name),"newer metadata lost");
  check(java.util.Arrays.equals(Files.readAllBytes(draft.file.toPath()),new byte[]{(byte)value}),"newer bytes corrupted");
 }
 public static void main(String[] args)throws Exception {
  android.content.Context ctx=new android.content.Context(new File(args[0]));String who="recipient";
  for(String mode:new String[]{"newer","removed","failed-newer","sending"}){
   MmsDraft.save(ctx,who,new byte[]{7},"image/jpeg","previous");
   Blocked stream=new Blocked();AtomicReference<Throwable> failure=new AtomicReference<>();
   Thread old=new Thread(()->{try{MmsDraft.save(ctx,who,stream,"image/jpeg","old");}
     catch(Throwable e){failure.set(e);}});old.start();
   check(stream.entered.await(2,TimeUnit.SECONDS),"old copy never started");
   if(mode.equals("removed"))MmsDraft.remove(ctx,who);
   else if(mode.equals("sending"))MmsDraft.state(ctx,MmsDraft.key(who),MmsDraft.SENDING,"");
   else if(mode.equals("failed-newer")){
    try{MmsDraft.save(ctx,who,new InputStream(){public int read()throws IOException{throw new IOException("revoked");}},"image/jpeg","failed");throw new AssertionError("expected failure");}
    catch(IOException expected){}
   }else MmsDraft.save(ctx,who,new byte[]{22},"image/png","newest");
   MmsDraft.setText(ctx,who,"new typed caption");
   // Another conversation also remains responsive while the first provider is blocked.
   MmsDraft.save(ctx,"other",new byte[]{33},"image/png","other");
   stream.release.countDown();old.join(2000);check(!old.isAlive(),"old worker leaked");
   check(failure.get() instanceof IOException,"superseded copy must not claim success");
   if(mode.equals("removed"))check(MmsDraft.load(ctx,who)==null,"deleted draft resurrected");
   else expect(ctx,who,mode.equals("newer")?"newest":"previous",mode.equals("newer")?22:7);
   check(MmsDraft.text(ctx,who).equals("new typed caption"),"old copy rewrote text");
   File[] pending=new File(ctx.getFilesDir(),"mms-drafts").listFiles((d,n)->n.endsWith(".tmp"));
   check(pending.length==0,"temporary copies leaked");
  }
  // Reserve ownership before openInputStream: an old provider opening slowly must not become newest.
  MmsDraft.Copy old=MmsDraft.beginCopy(who);
  MmsDraft.save(ctx,who,new byte[]{44},"image/png","latest-open");
  try{MmsDraft.save(ctx,who,new ByteArrayInputStream(new byte[]{55}),"image/png","late-open",old);throw new AssertionError("late open won");}
  catch(IOException expected){}
  expect(ctx,who,"latest-open",44);
  ThreadActivity.checkCallbacks(new File(args[0]));
 }
}''',
    }
    thread = (ROOT / 'mobile/android/app/src/main/java/place/poster/app/sms/ThreadActivity.java').read_text()
    method = _method(thread, '    private void stageAttachment(final Uri uri, final String mime, final String name, final Runnable onReady)')
    sources['place/poster/app/sms/ThreadActivity.java'] = r'''package place.poster.app.sms;
import java.io.*;import java.util.concurrent.*;
class ThreadActivity extends android.content.Context {
 String address="callback-recipient";boolean stagingAttachment,destroyed,finishing;Object attachment;
 int painted,notified,accepted;final Main main=new Main();
 ThreadActivity(File directory){super(directory);}
 boolean attachmentBusy(){return stagingAttachment;}boolean isDestroyed(){return destroyed;}boolean isFinishing(){return finishing;}
 void say(String text){notified++;}String getString(int id){return "ready";}void restoreAttachmentDraft(){painted++;}
 Resolver getContentResolver(){return new Resolver();}
 static class Resolver {InputStream openInputStream(Uri ignored){return new ByteArrayInputStream(new byte[]{66});}}
 static class Main {Runnable callback;CountDownLatch ready=new CountDownLatch(1);void post(Runnable task){callback=task;ready.countDown();}}
 METHOD
 static void checkCallbacks(File directory)throws Exception {
  for(String mode:new String[]{"destroyed","finishing","superseded","different-recipient","current"}){
   ThreadActivity activity=new ThreadActivity(directory);
   activity.stageAttachment(new Uri(),"image/jpeg","callback",()->activity.accepted++);
   if(!activity.main.ready.await(2,TimeUnit.SECONDS))throw new AssertionError("callback missing");
   activity.destroyed=mode.equals("destroyed");activity.finishing=mode.equals("finishing");
   if(mode.equals("superseded"))MmsDraft.beginCopy(activity.address);
   if(mode.equals("different-recipient"))activity.address="somebody-else";
   activity.main.callback.run();boolean current=mode.equals("current");
   if(activity.accepted!=(current?1:0)||activity.painted!=(current?1:0)||activity.notified!=(current?2:1))
    throw new AssertionError(mode+": stale Activity changed caption/UI");
  }
 }
}
class Uri {}
class R {static class string {static final int sms_attachment_ready=1;}}
'''.replace('METHOD', method)
    for name, body in sources.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    draft = ROOT / 'mobile/android/app/src/main/java/place/poster/app/sms/MmsDraft.java'
    built = subprocess.run(['javac', '-d', str(tmp_path), str(draft),
                            *[str(tmp_path / name) for name in sources]], capture_output=True, text=True, timeout=20)
    assert built.returncode == 0, built.stderr
    data = tmp_path / 'data'; data.mkdir()
    ran = subprocess.run(['java', '-cp', str(tmp_path), 'place.poster.app.sms.Probe', str(data)],
                         capture_output=True, text=True, timeout=15)
    assert ran.returncode == 0, ran.stdout + ran.stderr
