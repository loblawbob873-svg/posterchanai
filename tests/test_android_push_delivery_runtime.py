"""Execute native production callbacks: readiness, ACK after rendering, replay, and timeout."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / 'mobile/android/app/src/main/java/place/poster/app/push'


def test_socket_requires_ready_and_only_acknowledges_rendered_durable_messages(tmp_path):
    source = (JAVA / 'DirectPushService.java').read_text()
    callbacks = source[source.index('            @Override public void onOpen('):source.index('            @Override public void onClosing(')].replace('@Override ', '')
    ack = source[source.index('    private static void sendAck('):source.index('    private void failed(')]
    harness = r'''
import java.util.*;
public class DirectPushService {
 static class JSONObject {
   Map<String,Object> data=new HashMap<>();
   JSONObject(){} JSONObject(String text){
     String[] fields=text.split(":");data.put("type",fields[0]);
     if(fields.length>1){data.put("id",fields[1]);data.put("payload",new JSONObject());}
   }
   JSONObject put(String key,Object value){data.put(key,value);return this;}
   String optString(String key,String fallback){return String.valueOf(data.getOrDefault(key,fallback));}
   JSONObject optJSONObject(String key){return (JSONObject)data.get(key);}
   public String toString(){if(data.containsKey("id") && !(data.get("id") instanceof Long))throw new AssertionError("ACK id is not numeric");return data.toString();}
 }
 static class WebSocket {
   int acks=0,cancels=0;boolean send(String value){if(value.contains("type=ack"))acks++;return true;}
   void close(int code,String reason){} void cancel(){cancels++;}
 }
 static class Response {}
 static class Handler { List<Runnable> tasks=new ArrayList<>();void postDelayed(Runnable r,long ms){tasks.add(r);} void fire(){for(Runnable r:new ArrayList<>(tasks))r.run();tasks.clear();} }
 static class DirectPushStore {
   static Set<String> delivered=new HashSet<>();static boolean writable=true;
   static boolean wasDelivered(DirectPushService s,String id){return delivered.contains(id);}
   static boolean markDelivered(DirectPushService s,String id){if(writable)delivered.add(id);return writable;}
 }
 static class PushEventService {static int draws=0;static boolean allowed=true;static boolean deliver(DirectPushService s,String text){if(!allowed)return false;draws++;return true;} }
 static class RunningNote {static void refresh(DirectPushService s){} }
 static class Credentials {String token="private-test-token";}
 int generation=1,failures=4;boolean running=true,connected=false;String lastError="";
 static final int MAX_MESSAGE_BYTES=65536;Handler handler=new Handler();
 class Listener {int mine=1;Credentials credentials=new Credentials();
 CALLBACKS
 }
 ACK_METHOD
 static void expect(boolean result,String reason){if(!result)throw new AssertionError(reason);}
 public static void main(String[] args){
   DirectPushService service=new DirectPushService();Listener listener=service.new Listener();WebSocket ws=new WebSocket();
   listener.onOpen(ws,new Response());expect(!service.connected,"upgrade incorrectly reported authenticated");
   listener.onMessage(ws,"notification:1");expect(PushEventService.draws==0,"rendered before authentication");
   listener.onMessage(ws,"ready");expect(service.connected&&service.failures==0,"server readiness missing");
   service.handler.fire();expect(ws.cancels==0,"authenticated connection timed out");
   PushEventService.allowed=false;listener.onMessage(ws,"notification:1");expect(ws.acks==0,"blocked message was ACKed");
   PushEventService.allowed=true;listener.onMessage(ws,"notification:1");expect(ws.acks==1&&PushEventService.draws==1,"successful render was not ACKed");
   listener.onMessage(ws,"notification:1");expect(ws.acks==2&&PushEventService.draws==1,"replay rendered twice");
   DirectPushStore.writable=false;listener.onMessage(ws,"notification:2");expect(ws.acks==2,"failed dedupe persistence was ACKed");
   service.generation=2;listener.onMessage(ws,"notification:3");expect(ws.acks==2,"stale socket acted");
   DirectPushService pending=new DirectPushService();WebSocket waiting=new WebSocket();pending.new Listener().onOpen(waiting,new Response());
   pending.handler.fire();expect(waiting.cancels==1,"missing server ready never timed out");
 }
}
'''.replace('CALLBACKS', callbacks).replace('ACK_METHOD', ack)
    path = tmp_path / 'DirectPushService.java'
    path.write_text(harness)
    result = subprocess.run(['javac', str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    result = subprocess.run(['java', '-cp', str(tmp_path), 'DirectPushService'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_reconnect_backoff_cannot_outlast_the_call_queue(tmp_path):
    source = (JAVA / 'DirectPushService.java').read_text()
    retry = source[source.index('    private void failed('):source.index('    private void stopDirect(')]
    constant = next(line for line in source.splitlines() if 'private static final long MAX_BACKOFF_MS =' in line)
    harness = r'''
public class DirectPushService {
 CONSTANT
 int generation=2,failures=0,reconnected=0;boolean running=true,connected=true,configured=true;
 String lastError="";WebSocket socket=new WebSocket();Handler handler=new Handler();
 static class WebSocket {}
 static class Handler {long delay=-1;Runnable job;void removeCallbacksAndMessages(Object o){job=null;}void postDelayed(Runnable r,long d){job=r;delay=d;} }
 static class RunningNote {static void refresh(DirectPushService s){} }
 static class DirectPushStore {static String deviceId(DirectPushService s){return "phone-test-123456789";} }
 static boolean configured(DirectPushService s){return s.configured;}
 void connectNow(){reconnected++;}
 RETRY
 static void check(boolean b){if(!b)throw new AssertionError();}
 public static void main(String[] args){
   DirectPushService s=new DirectPushService();
   s.failed(1,s.socket,"stale");check(s.connected&&s.handler.job==null);
   long previous=0;
   for(int i=0;i<20;i++){
     s.socket=new WebSocket();s.failed(2,s.socket,"radio unavailable");
     check(!s.connected&&s.socket==null&&s.handler.job!=null);
     check(s.handler.delay>=previous&&s.handler.delay<31000);previous=s.handler.delay;
     s.handler.job.run();
   }
   check(s.reconnected==20);
   s.handler.job=null;s.running=false;s.failed(2,null,"disabled");check(s.handler.job==null);
   s.running=true;s.configured=false;s.failed(2,null,"credentials removed");check(s.handler.job==null);
 }
}
'''.replace('CONSTANT', constant).replace('RETRY', retry)
    path = tmp_path / 'DirectPushService.java'
    path.write_text(harness)
    compiled = subprocess.run(['javac', str(path)], capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr
    ran = subprocess.run(['java', '-cp', str(tmp_path), 'DirectPushService'], capture_output=True, text=True)
    assert ran.returncode == 0, ran.stderr
