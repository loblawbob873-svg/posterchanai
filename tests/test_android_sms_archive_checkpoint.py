"""Execute shipped checkpoint serialization/commit/rescan blocks with an atomic preferences store."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / 'mobile/android/app/src/main/java/place/poster/app'


def method(source, signature):
    start = source.index(signature)
    opening = source.index('{', start)
    depth = 1
    end = opening + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


def test_checkpoint_restart_rescan_race_and_owner_isolation(tmp_path):
    source = (JAVA / 'sms/SmsArchive.java').read_text()
    methods = '\n'.join(method(source, sig) for sig in [
        'private static String markKey(', 'private static String pendingKey(',
        'public static long mark(', 'public static void rescan(',
        'private static String encode(', 'private static SmsSweep.Report restore(', 'private static SmsSweep.Report durableRestore(',
        'public static boolean current(', 'public static void commit('])
    stage = source[source.index('        rep.owner = pubHex;'):source.index('        record(ctx, rep);')]
    harness = r'''
import java.util.*;
import java.util.concurrent.*;
import place.poster.app.sync.Json;
public class SmsArchive {
 static final Object CHECKPOINT_LOCK=new Object();static final int ROWS_PER_PASS=25;
 static class Context {String owner="a";Prefs prefs=new Prefs();}
 static class Prefs {Map<String,Object>data=new HashMap<>();boolean writable=true;CountDownLatch entered,release;
  long getLong(String k,long d){return ((Number)data.getOrDefault(k,d)).longValue();}
  String getString(String k,String d){return (String)data.getOrDefault(k,d);}
  Editor edit(){return new Editor();}
  class Editor {Map<String,Object>updates=new HashMap<>();Set<String>removed=new HashSet<>();
   Editor putLong(String k,long v){updates.put(k,v);return this;}Editor putString(String k,String v){updates.put(k,v);return this;}Editor remove(String k){removed.add(k);return this;}
   boolean commit(){if(entered!=null&&updates.keySet().stream().anyMatch(k->k.startsWith("pending."))){entered.countDown();try{release.await();}catch(Exception e){throw new RuntimeException(e);}entered=null;}data.keySet().removeAll(removed);data.putAll(updates);return writable;}
  }
 }
 static Prefs prefs(Context c){return c.prefs;}static String owner(Context c){return c.owner;}static void note(Context c,String n){}
 static class JSONObject {Map<String,Object>data;JSONObject(){data=new LinkedHashMap<>();}JSONObject(String s){data=Json.obj(Json.parse(s));}
  JSONObject put(String k,Object v){data.put(k,v instanceof JSONArray?((JSONArray)v).data:v instanceof JSONObject?((JSONObject)v).data:v);return this;}
  String optString(String k){return optString(k,"");}String optString(String k,String d){return Json.str(data.get(k),d);}long optLong(String k,long d){return Json.num(data.get(k),d);}boolean optBoolean(String k,boolean d){return Json.bool(data.get(k),d);}
  JSONArray getJSONArray(String k){JSONArray a=new JSONArray();a.data=Json.arr(data.get(k));return a;}public String toString(){return Json.write(data);}}
 static class JSONArray {List<Object>data=new ArrayList<>();JSONArray put(JSONObject o){data.add(o.data);return this;}int length(){return data.size();}JSONObject getJSONObject(int i){return new JSONObject(Json.write(data.get(i)));}}
 static class SmsSweep {static class Report {String owner="",error="";long mark,revision,smsAtMark=-1,mmsAtMark=-1;boolean more;int skipped;List<JSONObject>events=new ArrayList<>();}}
 METHODS
 static SmsSweep.Report stage(Context ctx,SmsSweep.Report rep,String pubHex,long revision){STAGE return rep;}
 static SmsSweep.Report report(String owner,String id)throws Exception {SmsSweep.Report r=new SmsSweep.Report();r.owner=owner;r.mark=1800000000000L;r.smsAtMark=30;r.mmsAtMark=8;r.more=true;r.events.add(new JSONObject().put("id",id.repeat(64)).put("pubkey",owner).put("content","opaque ciphertext"));return r;}
 static void check(boolean b,String m){if(!b)throw new AssertionError(m);}
 public static void main(String[]args)throws Exception{
  Context c=new Context();SmsSweep.Report original=report("a","b");
  check(stage(c,original,"a",0)!=null,"pending persistence failed");check(mark(c)==0,"prepare advanced cursor");
  String stored=prefs(c).getString(pendingKey("a"),"");SmsSweep.Report loaded=restore(stored,"a");
  check(encode(loaded).equals(stored),"restart changed signed bytes/checkpoint");check(loaded.smsAtMark==30&&loaded.mmsAtMark==8,"boundary ids lost in restart");
  Context fresh=new Context();fresh.prefs.data.put(pendingKey("a"),stored);check(current(fresh,loaded),"restored checkpoint not current");commit(fresh,loaded);check(mark(fresh)==original.mark&&fresh.prefs.getLong("sms.v3:a",-1)==30,"ack did not atomically advance");check(fresh.prefs.getString(pendingKey("a"),"").isEmpty(),"ack left pending checkpoint");
  c.owner="other";commit(c,loaded);check(mark(c)==0&&!current(c,loaded),"cross-account receipt advanced history");c.owner="a";
  rescan(c);commit(c,loaded);check(mark(c)==0&&c.prefs.getString(pendingKey("a"),"").isEmpty(),"delayed receipt undid rescan");
  check(stage(c,original,"a",0)==null,"old revision persisted after rescan");
  Context failed=new Context();failed.prefs.writable=false;check(stage(failed,report("a","c"),"a",0)==null,"failed durability allowed publication");
  String memoryPending=failed.prefs.getString(pendingKey("a"),"");check(!memoryPending.isEmpty(),"fixture must model Android memory-first failed commit");
  boolean refused=false;try{durableRestore(failed,"a",memoryPending);}catch(Exception expected){refused=true;}check(refused,"memory-visible failed checkpoint escaped without durability");
  failed.prefs.writable=true;check(durableRestore(failed,"a",memoryPending)!=null,"restored checkpoint did not recover after disk became writable");
  // Pause the actual pending commit while a rescan races. Rescan must serialize AFTER it,
  // then remove that pending record; the old worker must never recreate it after the reset.
  Context race=new Context();race.prefs.entered=new CountDownLatch(1);race.prefs.release=new CountDownLatch(1);
  Thread writer=new Thread(()->{try{stage(race,report("a","d"),"a",0);}catch(Exception e){throw new RuntimeException(e);}});
  writer.start();check(race.prefs.entered.await(2,TimeUnit.SECONDS),"writer did not enter checkpoint");
  CountDownLatch resetStarted=new CountDownLatch(1),resetFinished=new CountDownLatch(1);Thread reset=new Thread(()->{resetStarted.countDown();rescan(race);resetFinished.countDown();});reset.start();resetStarted.await();
  boolean resetCrossedLock=resetFinished.await(100,TimeUnit.MILLISECONDS);race.prefs.release.countDown();check(!resetCrossedLock,"rescan crossed active checkpoint lock");writer.join(2000);reset.join(2000);check(!writer.isAlive()&&!reset.isAlive(),"checkpoint deadlock");
  check(race.prefs.getLong("revision.v3:a",0)==1&&race.prefs.getString(pendingKey("a"),"").isEmpty(),"old checkpoint survived racing rescan");
 }
}
'''.replace('METHODS', methods).replace('STAGE', stage)
    driver = tmp_path / 'SmsArchive.java'
    driver.write_text(harness)
    compiled = subprocess.run(['javac', '-d', str(tmp_path), str(JAVA / 'sync/Json.java'), str(driver)], capture_output=True, text=True, timeout=30)
    assert compiled.returncode == 0, compiled.stderr
    ran = subprocess.run(['java', '-cp', str(tmp_path), 'SmsArchive'], capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stderr
