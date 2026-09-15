"""Run shipped native archive delivery callbacks: relay receipts, retries and account boundaries."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / 'mobile/android/app/src/main/java/place/poster/app'


def test_every_archive_document_needs_relay_acceptance_before_advancing(tmp_path):
    source = (JAVA / 'signer/SignerRelayService.java').read_text()
    methods = source[source.index('    private void sweepSmsHistory()'):source.index('    /** Try both schemes')]
    harness = r'''
import java.util.*;
public class SignerRelayService {
 static class JSONObject {String id;JSONObject(String i){id=i;}String optString(String k,String d){return id;}}
 static class JSONArray {String id;JSONArray put(Object v){if(v instanceof JSONObject)id=((JSONObject)v).id;return this;}public String toString(){return id;}}
 static class SmsSweep {static class Report {String owner="a",error="";int skipped;long mark=123;boolean more;List<JSONObject>events=new ArrayList<>();}}
 static class SmsArchive {static final int ROWS_PER_PASS=25;static SmsSweep.Report disk;static int commits;static long cursor;
  static SmsSweep.Report sweep(SignerRelayService s,int n){return disk;}
  static boolean current(SignerRelayService s,SmsSweep.Report r){return r==disk;}
  static void commit(SignerRelayService s,SmsSweep.Report r){commits++;cursor=r.mark;}}
 static class WebSocket {List<String>sent=new ArrayList<>();boolean queued=true;boolean send(String s){sent.add(s);return queued;}}
 static class Handler {List<Runnable>later=new ArrayList<>();void post(Runnable r){r.run();}void postDelayed(Runnable r,long ms){later.add(r);}}
 static class Pool {List<Runnable>jobs=new ArrayList<>();void execute(Runnable r){jobs.add(r);}void finish(){jobs.remove(0).run();}}
 Handler handler=new Handler();Pool pool=new Pool();Pool pool(){return pool;}
 Map<String,WebSocket>socks=new HashMap<>();String owner="a",lastError="";boolean stopping;
 byte[] sec(){return new byte[]{1};}String myPub(byte[] key){return owner;}
 SmsSweep.Report smsHistoryBatch;place.poster.app.sms.SmsArchiveDelivery smsHistoryDelivery;
 boolean smsHistoryBuilding,smsHistoryRetryScheduled;Object smsHistoryGeneration=new Object();
 METHODS
 static void check(boolean b,String m){if(!b)throw new AssertionError(m);}
 static SignerRelayService start(){SignerRelayService s=new SignerRelayService();s.socks.put("relay",new WebSocket());return s;}
 public static void main(String[]args){
  SmsArchive.disk=new SmsSweep.Report();for(String id:Arrays.asList("one","two","three"))SmsArchive.disk.events.add(new JSONObject(id));
  SignerRelayService s=start();s.sweepSmsHistory();s.sweepSmsHistory();check(s.pool.jobs.size()==1,"concurrent sweeps prepared duplicate batches");s.pool.finish();
  check(SmsArchive.cursor==0,"socket enqueue advanced history");check(s.socks.get("relay").sent.size()==3,"not every document was transmitted");
  s.acceptSmsHistoryAck("one",false);s.acceptSmsHistoryAck("unrelated",true);check(SmsArchive.cursor==0,"denial/unrelated OK advanced history");
  s.acceptSmsHistoryAck("one",true);s.acceptSmsHistoryAck("one",true);s.acceptSmsHistoryAck("two",true);check(SmsArchive.cursor==0,"partial ACK advanced entire batch");
  WebSocket replacement=new WebSocket();s.socks.put("relay",replacement);s.flushSmsHistory();check(replacement.sent.equals(Arrays.asList("three")),"reconnect did not retry outstanding exact event");
  s.acceptSmsHistoryAck("three",true);check(SmsArchive.cursor==123&&SmsArchive.commits==1,"complete ACK set did not commit");
  s.acceptSmsHistoryAck("three",true);check(SmsArchive.commits==1,"duplicate OK committed twice");
  // A restarted service replays the durable batch; previous in-memory OKs do not imply delivery.
  SmsArchive.cursor=0;SignerRelayService restored=start();restored.sweepSmsHistory();restored.pool.finish();check(restored.socks.get("relay").sent.equals(Arrays.asList("one","two","three")),"restart lost exact pending event ids");
  restored.owner="b";restored.acceptSmsHistoryAck("one",true);restored.flushSmsHistory();check(restored.smsHistoryBatch==null&&SmsArchive.cursor==0,"old account batch escaped ownership");
  SignerRelayService delayed=start();delayed.sweepSmsHistory();delayed.smsHistoryGeneration=new Object();delayed.owner="b";delayed.pool.finish();check(delayed.socks.get("relay").sent.isEmpty(),"stale signing callback published after account change");
  SmsArchive.disk=null;SignerRelayService failed=start();failed.sweepSmsHistory();failed.pool.finish();check(failed.smsHistoryRetryScheduled,"transient build failure has no retry");
  SmsArchive.disk=new SmsSweep.Report();SignerRelayService empty=start();empty.sweepSmsHistory();empty.pool.finish();check(!empty.smsHistoryRetryScheduled,"empty completed history hotloops");
  SmsArchive.disk.error="provider unavailable";SignerRelayService denied=start();denied.sweepSmsHistory();denied.pool.finish();check(denied.smsHistoryRetryScheduled,"provider refusal lost its retry");
  SignerRelayService offline=new SignerRelayService();offline.sweepSmsHistory();check(offline.pool.jobs.isEmpty(),"offline sweep began");
 }
}
'''.replace('METHODS', methods)
    driver = tmp_path / 'SignerRelayService.java'
    driver.write_text(harness)
    compiled = subprocess.run(['javac', '-d', str(tmp_path), str(JAVA / 'sms/SmsArchiveDelivery.java'), str(driver)], capture_output=True, text=True, timeout=30)
    assert compiled.returncode == 0, compiled.stderr
    ran = subprocess.run(['java', '-cp', str(tmp_path), 'SignerRelayService'], capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stderr
