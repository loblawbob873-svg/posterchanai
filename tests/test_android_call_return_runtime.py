"""Execute actual Phone return-control methods without placing a call; device tests cover lifecycle."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_actual_return_control_handles_live_ended_and_disappeared_service(tmp_path):
    source = (ROOT / 'mobile/android/app/src/main/java/place/poster/app/phone/DialerActivity.java').read_text()
    start = source.index('    void updateReturnToCall()')
    methods = source[start:source.index('    /** Open this person', start)]
    java = '''
import java.util.*;
public class DialerActivity {
 static class View {static int VISIBLE=0,GONE=8; int visibility=GONE;void setVisibility(int v){visibility=v;}}
 static class InCallActivity {}
 static class Intent {static int FLAG_ACTIVITY_NEW_TASK=1,FLAG_ACTIVITY_SINGLE_TOP=2;Class<?> target;int flags;
   Intent(Object ctx,Class<?> target){this.target=target;}Intent addFlags(int v){flags=v;return this;}}
 static class PcInCallService {static PcInCallService INSTANCE;boolean active;
   List<Object> liveCalls(){return active?Collections.singletonList(new Object()):Collections.emptyList();}}
 View returnToCall=new View();int launches=0;Intent launched;
 void startActivity(Intent i){launches++;launched=i;}
 METHODS
 static void check(boolean ok){if(!ok)throw new AssertionError();}
 public static void main(String[] args){
  DialerActivity a=new DialerActivity();a.updateReturnToCall();check(a.returnToCall.visibility==View.GONE);
  a.returnToCall();check(a.launches==0);
  PcInCallService s=new PcInCallService();PcInCallService.INSTANCE=s;s.active=true;
  a.updateReturnToCall();check(a.returnToCall.visibility==View.VISIBLE);a.returnToCall();
  check(a.launches==1&&a.launched.target==InCallActivity.class&&a.launched.flags==3);
  s.active=false;a.returnToCall();check(a.launches==1&&a.returnToCall.visibility==View.GONE);
  s.active=true;a.updateReturnToCall();check(a.returnToCall.visibility==View.VISIBLE);
  PcInCallService.INSTANCE=null;a.returnToCall();check(a.launches==1&&a.returnToCall.visibility==View.GONE);
 }
}
'''.replace('METHODS', methods)
    file = tmp_path / 'DialerActivity.java'
    file.write_text(java)
    result = subprocess.run(['javac', str(file)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    result = subprocess.run(['java', '-cp', str(tmp_path), 'DialerActivity'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
