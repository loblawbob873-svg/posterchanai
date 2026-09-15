"""Run the actual SMS provider query: null/partial reads cannot masquerade as complete history."""
from pathlib import Path
import subprocess
from tests.test_android_sms_archive_checkpoint import method

ROOT = Path(__file__).resolve().parents[1]
JAVA = ROOT / 'mobile/android/app/src/main/java/place/poster/app/sms'


def test_archive_provider_failure_is_local_to_the_querying_thread(tmp_path):
    source = (JAVA / 'SmsStore.java').read_text()
    query = method(source, 'private static List<SmsMsg> query(')
    harness = r'''
import java.util.*;
import java.util.concurrent.*;
public class SmsStore {
 static final String TAG="test";static final String[]COLS={};static volatile boolean refused;
 static final ThreadLocal<Boolean>reactionReadComplete=new ThreadLocal<>(),archiveReadFailure=new ThreadLocal<>();
 static class Log {static void w(String t,String m,Throwable e){}}
 static class Telephony {static class Sms {static final String CONTENT_URI="sms";}}
 static class SmsMsg {long id,threadId,date;String address,body;int type;boolean read;}
 static class Context {String mode;Context(String m){mode=m;}Resolver getContentResolver(){return new Resolver(mode);}}
 static class Resolver {String mode;Resolver(String m){mode=m;}Cursor query(String u,String[]cols,String w,String[]a,String order){if(mode.equals("throw"))throw new RuntimeException();return mode.equals("null")?null:new Cursor(mode);}}
 static class Cursor {int row=-1;String mode;Cursor(String m){mode=m;}boolean moveToNext(){row++;if(row==1&&mode.equals("partial"))throw new RuntimeException();return row<2;}long getLong(int col){return row+1;}int getInt(int col){return 1;}String getString(int col){if(col==2&&mode.equals("address"))throw new RuntimeException();return col==2?"+1555":"message";}void close(){}}
 static String str(Cursor c,int i){try{return c.getString(i);}catch(Throwable t){return "";}}
 QUERY
 static void check(boolean b,String m){if(!b)throw new AssertionError(m);}
 public static void main(String[]args)throws Exception{
  for(String mode:Arrays.asList("null","throw","partial","address")){
   List<SmsMsg>rows=query(new Context(mode),"",new String[]{},"date ASC",25);
   check(Boolean.TRUE.equals(archiveReadFailure.get()),"failed "+mode+" read looked complete");
   if(mode.equals("partial"))check(rows.size()==1,"foreground partial row was unnecessarily erased");
  }
  CountDownLatch failed=new CountDownLatch(1),otherDone=new CountDownLatch(1);List<Throwable>errors=new ArrayList<>();
  Thread archive=new Thread(()->{try{query(new Context("throw"),"",new String[]{},"date ASC",25);failed.countDown();otherDone.await();check(Boolean.TRUE.equals(archiveReadFailure.get()),"foreground success erased archive failure");}catch(Throwable e){errors.add(e);}});
  archive.start();failed.await();query(new Context("ok"),"",new String[]{},"date ASC",25);check(!archiveReadFailure.get(),"successful query retained old error");otherDone.countDown();archive.join();check(errors.isEmpty(),errors.toString());
 }
}
'''.replace('QUERY', query)
    driver = tmp_path / 'SmsStore.java'
    driver.write_text(harness)
    compiled = subprocess.run(['javac', str(driver)], capture_output=True, text=True, timeout=30)
    assert compiled.returncode == 0, compiled.stderr
    ran = subprocess.run(['java', '-cp', str(tmp_path), 'SmsStore'], capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stderr
