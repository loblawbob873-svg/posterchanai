"""Run the shipped notification-readiness method against Android permission/channel states.

Android framework stand-ins supply only state; the decision is production Java, not a copy.
The instrumented counterpart also checks real NotificationManager/shade delivery on Android.
"""
from pathlib import Path
import shutil
import subprocess
import pytest
from tests.test_android_launch_view import method

@pytest.mark.skipif(not shutil.which('javac'), reason='JDK is required')
def test_notification_readiness_checks_permission_app_and_channel(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = (root / 'mobile/android/app/src/main/java/place/poster/app/sms/SmsNotifier.java').read_text()
    body = method(source, 'public static boolean canNotify')
    (tmp_path / 'android/content/pm').mkdir(parents=True)
    (tmp_path / 'android/content/pm/PackageManager.java').write_text(
        'package android.content.pm; public class PackageManager { public static final int PERMISSION_GRANTED=0; }')
    (tmp_path / 'Harness.java').write_text('''
class Build { static class VERSION { static int SDK_INT=34; } static class VERSION_CODES { static int O=26; } }
class NotificationChannel { int importance; NotificationChannel(int value){importance=value;} int getImportance(){return importance;} }
class NotificationManager {
 static final int IMPORTANCE_NONE=0; boolean enabled=true; NotificationChannel channel;
 boolean areNotificationsEnabled(){return enabled;} NotificationChannel getNotificationChannel(String id){return channel;}
}
class Context {
 static final String NOTIFICATION_SERVICE="notification";
 int permission=0; NotificationManager manager=new NotificationManager();
 int checkSelfPermission(String permissionName){return permission;}
 Object getSystemService(String name){return manager;}
}
public class Harness {
 static final String CHANNEL="pcai_sms";
''' + body + '''
 static void expect(Context context, boolean expected, String name){
   if(canNotify(context)!=expected)throw new AssertionError(name);
 }
 public static void main(String[] ignored){
   Context c=new Context(); expect(c,true,"permission granted, channel not created yet");
   c.permission=-1; expect(c,false,"runtime permission refused");
   Build.VERSION.SDK_INT=32; expect(c,true,"older Android has no notification runtime permission");
   Build.VERSION.SDK_INT=34; c.permission=0;
   c.manager.enabled=false; expect(c,false,"all app notifications muted");
   c.manager.enabled=true; c.manager.channel=new NotificationChannel(0);
   expect(c,false,"SMS channel muted despite app permission");
   c.manager.channel.importance=4; expect(c,true,"SMS channel enabled");
   c.manager=null; expect(c,false,"notification manager unavailable");
 }
}
''')
    compiled = subprocess.run(['javac', '-d', str(tmp_path), str(tmp_path/'Harness.java'),
                              str(tmp_path/'android/content/pm/PackageManager.java')],
                             capture_output=True, text=True, timeout=60)
    assert compiled.returncode == 0, compiled.stderr
    ran = subprocess.run(['java', '-cp', str(tmp_path), 'Harness'], capture_output=True, text=True, timeout=30)
    assert ran.returncode == 0, ran.stderr
