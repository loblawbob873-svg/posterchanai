package android.app;

import android.content.Context;
import android.content.Intent;
import android.os.IBinder;

public abstract class Service extends Context {
  public static final int START_STICKY = 1;
  public static final int START_NOT_STICKY = 2;
  public abstract IBinder onBind(Intent intent);
  public int onStartCommand(Intent intent, int flags, int startId) { return START_STICKY; }
  public void onCreate() { }
  public void onDestroy() { }
  public void onTimeout(int startId) { }
  public void onTimeout(int startId, int fgsType) { }
  public void stopSelf() { }
  public android.content.ContentResolver getContentResolver() { return null; }
  public android.content.SharedPreferences getSharedPreferences(String n, int m) { return null; }
}
