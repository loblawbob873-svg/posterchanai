"""Execute each actual provider query with interrupted/concurrent cursor boundaries."""
from pathlib import Path
import re
import subprocess
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('store', ['SmsStore', 'MmsStore'])
def test_reaction_snapshot_completeness_belongs_to_its_read(tmp_path, store):
    source = (ROOT / 'mobile/android/app/src/main/java/place/poster/app/sms' / (store + '.java')).read_text()
    start = source.index('    private static List<SmsMsg> query(')
    query = source[start:source.index('\n    }', start) + 6]
    fields = '\n'.join(re.findall(r'^    private static (?:final ThreadLocal<Boolean>|volatile boolean).*$', source, re.M))
    # Baseline uses exactly the old reactionHistory predicate; candidate uses its explicit read API.
    read = re.search(r'    static boolean reactionReadComplete\(\) \{[^\n]+', source)
    accessor = read.group(0) if read else 'static boolean reactionReadComplete(){return !refused' + (' && !capped' if store == 'MmsStore' else '') + ';}'
    body_failure = 'query(new Context(4),null,null,"date DESC",501);check(!reactionReadComplete(),"unreadable body may hide duplicate targets");' if store == 'SmsStore' else ''
    harness = r'''
import java.util.*;import java.util.concurrent.*;
public class Probe {
 static final String TAG="fixture";static final int MAX_ROWS=2000,PDU_NOTIFICATION_IND=130;
 static final String[] COLS={};
 static class Telephony {static class Sms{static Object CONTENT_URI;}static class Mms{static Object CONTENT_URI;}}
 static class Log {static void w(String t,String m,Throwable x){}}
 static class SmsMsg {boolean mms,read,undownloaded;long id,threadId,date;int type;String address,body="",error;boolean failed(){return false;}boolean pending(){return false;}}
 static class MmsWhen {static long millis(long n){return n;}}
 static class MmsFailures {static String get(Context c,long id){return "";}}
 static int box(int n){return n;}static String str(Cursor c,int i){return "text";}
 static void fillParts(Context c,List<SmsMsg> m){}static void fillAddresses(Context c,List<SmsMsg> m){}
 static class Context {int mode;Context(int m){mode=m;}Context getContentResolver(){return this;}
  Cursor query(Object uri,String[] cols,String where,String[] args,String order){if(mode==1)throw new SecurityException();return mode==2?null:new Cursor(mode);}}
 static class Cursor {int mode,step;Cursor(int m){mode=m;}boolean moveToNext(){if(mode==3&&step++>0)throw new IllegalStateException("cursor lost");return mode==3||mode==4;}String getString(int i){if(mode==4)throw new IllegalStateException("body lost");return "text";}long getLong(int i){return 1;}int getInt(int i){return 1;}void close(){}}
''' + fields + '\n' + accessor + '\n' + query + r'''
 static List<String> failures=new ArrayList<>();
 static void check(boolean b,String why){if(!b)failures.add(why);}
 public static void main(String[]args)throws Exception{
  query(new Context(0),null,null,"date DESC",501);check(reactionReadComplete(),"healthy empty snapshot");
  query(new Context(2),null,null,"date DESC",501);check(!reactionReadComplete(),"null cursor is not complete history");
  query(new Context(3),null,null,"date DESC",501);check(!reactionReadComplete(),"partial cursor is not complete history");
  query(new Context(0),null,null,"date DESC",501);query(null,null,null,"date DESC",501);check(!reactionReadComplete(),"null context must not reuse earlier success");
  CountDownLatch failed=new CountDownLatch(1),healthy=new CountDownLatch(1);boolean[] unsafe={true};
  Thread a=new Thread(()->{query(new Context(1),null,null,"date DESC",501);failed.countDown();try{healthy.await();}catch(Exception e){throw new RuntimeException(e);}unsafe[0]=reactionReadComplete();});
  Thread b=new Thread(()->{try{failed.await();}catch(Exception e){throw new RuntimeException(e);}query(new Context(0),null,null,"date DESC",501);healthy.countDown();});
  a.start();b.start();a.join(3000);b.join(3000);check(!a.isAlive()&&!b.isAlive(),"threads finish");check(!unsafe[0],"concurrent successful query erased this query's refusal");
  if(!failures.isEmpty())throw new AssertionError(failures.toString());
 }
}
'''
    harness = harness.replace('  CountDownLatch failed=', body_failure + '\n  CountDownLatch failed=')
    path = tmp_path / 'Probe.java'
    path.write_text(harness)
    built = subprocess.run(['javac', '-d', str(tmp_path), str(path)], capture_output=True, text=True, timeout=30)
    assert built.returncode == 0, built.stderr
    result = subprocess.run(['java', '-cp', str(tmp_path), 'Probe'], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
