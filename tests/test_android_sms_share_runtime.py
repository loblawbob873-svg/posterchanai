"""Execute shipped share intent policy against Android intent/URI value boundaries."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SMS = ROOT / 'mobile/android/app/src/main/java/place/poster/app/sms'


def test_camera_share_keeps_read_grant_and_consumes_each_intent_once(tmp_path):
    files = {
        'android/net/Uri.java': '''package android.net; public class Uri {
          final String value; public Uri(String s){value=s;} public String getScheme(){return value.split(":")[0];}}''',
        'android/content/ClipData.java': '''package android.content; import android.net.Uri;
          public class ClipData { final Uri uri; ClipData(Uri u){uri=u;}
          public static ClipData newRawUri(String label,Uri u){return new ClipData(u);}
          public int getItemCount(){return 1;} public Item getItemAt(int n){return new Item(uri);}
          public static class Item {Uri uri;Item(Uri u){uri=u;}public Uri getUri(){return uri;}}}''',
        'android/content/Intent.java': '''package android.content; import java.util.*;
          public class Intent {public static final String ACTION_SEND="SEND",EXTRA_STREAM="stream";
          public static final int FLAG_GRANT_READ_URI_PERMISSION=1,FLAG_GRANT_WRITE_URI_PERMISSION=2;
          String action,type;int flags;ClipData clip;Map<String,Object> extras=new HashMap<>();
          public String getAction(){return action;}public Intent setAction(String a){action=a;return this;}
          public String getType(){return type;}public Intent setType(String t){type=t;return this;}
          public Intent putExtra(String k,Object v){extras.put(k,v);return this;}
          @SuppressWarnings("unchecked") public <T>T getParcelableExtra(String k){return (T)extras.get(k);}
          public ClipData getClipData(){return clip;}public Intent setClipData(ClipData c){clip=c;return this;}
          public Intent addFlags(int f){flags|=f;return this;}public int getFlags(){return flags;}
          public void removeExtra(String k){extras.remove(k);}}''',
        'place/poster/app/sms/SmsShare.java': (SMS/'SmsShare.java').read_text(),
        'place/poster/app/sms/ShareProbe.java': '''package place.poster.app.sms;
          import android.content.*;import android.net.Uri;
          public class ShareProbe {
           static void check(boolean b,String why){if(!b)throw new AssertionError(why);}
           public static void main(String[] args){
            Uri uri=new Uri("content://camera/photo");
            Intent from=new Intent().setAction(Intent.ACTION_SEND).setType("image/jpeg")
              .putExtra(Intent.EXTRA_STREAM,uri).addFlags(3);
            Intent to=new Intent(); SmsShare.forward(from,to);
            check(SmsShare.stream(to)==uri,"stream survives activity handoff");
            check(to.getClipData().getItemAt(0).getUri()==uri,"grant URI in ClipData");
            check(to.getFlags()==1,"only read grant survives router finish");
            SmsShare.consumed(to);check(SmsShare.stream(to)==null,"recreation must not stage twice");
            Intent warm=new Intent();SmsShare.forward(from,warm);
            check(SmsShare.stream(warm)==uri,"new share of same photo must still work");
            from.removeExtra(Intent.EXTRA_STREAM);from.setClipData(ClipData.newRawUri("photo",uri));
            check(SmsShare.stream(from)==uri,"ClipData-only camera share");
            from.setType("video/mp4");check(SmsShare.stream(from)==uri,"gallery video");
            from.setType("text/plain");check(SmsShare.stream(from)==null,"text is not an image");
            from.setType("image/jpeg").putExtra(Intent.EXTRA_STREAM,new Uri("file:///private/secret"));
            check(SmsShare.stream(from)==null,"only granted content URIs");
            from.setAction("SEND_MULTIPLE");check(SmsShare.stream(from)==null,"never silently choose first of multiple");
            check(SmsShare.stream(null)==null,"no intent");
           }}''',
    }
    for name, source in files.items():
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(source)
    built=subprocess.run(['javac','-d',str(tmp_path),*[str(tmp_path/name) for name in files]],capture_output=True,text=True,timeout=20)
    assert built.returncode==0,built.stderr
    result=subprocess.run(['java','-cp',str(tmp_path),'place.poster.app.sms.ShareProbe'],capture_output=True,text=True,timeout=5)
    assert result.returncode==0,result.stdout+result.stderr
