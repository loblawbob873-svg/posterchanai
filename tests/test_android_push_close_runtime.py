"""Execute the actual Android socket close callback against revocation and stale races."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_actual_close_callback_handles_server_auth_code_and_ignores_old_socket(tmp_path):
    source = (ROOT / 'mobile/android/app/src/main/java/place/poster/app/push/DirectPushService.java').read_text()
    start = source.index('            @Override public void onClosed(')
    body = source[start:source.index('            @Override public void onFailure', start)].replace('@Override ', '')
    harness = '''
public class DirectPushService {
 interface WebSocket {}
 int generation=2, retries=0, stopped=0, removed=0; boolean running=true,connected=true;
 String lastError=""; WebSocket socket=new WebSocket(){};
 static class DirectPushStore { static int cleared=0; static void clear(DirectPushService s){cleared++;} }
 void failed(int mine,WebSocket ws,String why){retries++;}
 void stopSelf(){stopped++;} void dropNotification(){removed++;}
 class Listener { int mine=2;
 BODY
 }
 static void check(boolean ok){if(!ok)throw new AssertionError();}
 public static void main(String[] args){
   for(int code:new int[]{1008,4001,4003,4401,1000,1006,1011,4002}){
     for(boolean stale:new boolean[]{false,true}){
       DirectPushStore.cleared=0; DirectPushService s=new DirectPushService(); Listener l=s.new Listener();
       if(stale)l.mine=1;l.onClosed(s.socket,code,"closed");
       boolean revoked=code==1008||code==4001||code==4003||code==4401;
       check(DirectPushStore.cleared==(!stale&&revoked?1:0));
       check(s.retries==(!stale&&!revoked?1:0));
       check(s.stopped==(!stale&&revoked?1:0));
       if(!stale&&revoked){check(!s.running&&!s.connected&&s.socket==null);}
       else check(s.running&&s.connected&&s.socket!=null);
     }
   }
   DirectPushStore.cleared=0;DirectPushService s=new DirectPushService();s.running=false;
   s.new Listener().onClosed(s.socket,4401,"disabled");check(DirectPushStore.cleared==0);
 }
}
'''.replace('BODY', body)
    java = tmp_path / 'DirectPushService.java'
    java.write_text(harness)
    result = subprocess.run(['javac', str(java)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    result = subprocess.run(['java', '-cp', str(tmp_path), 'DirectPushService'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
