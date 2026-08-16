package android.content;

public abstract class Context {
  public static final int MODE_PRIVATE = 0;
  public static final String ALARM_SERVICE = "alarm";
  public static final String POWER_SERVICE = "power";
  public static final String BATTERY_SERVICE = "batterymanager";
  public static final String CONNECTIVITY_SERVICE = "connectivity";
  public static final String NOTIFICATION_SERVICE = "notification";
  public abstract ContentResolver getContentResolver();
  public abstract SharedPreferences getSharedPreferences(String name, int mode);
  public Context getApplicationContext() { return this; }
  public java.io.File getFilesDir() { return null; }
  public Object getSystemService(String name) { return null; }
  public String getPackageName() { return ""; }
  public android.content.pm.PackageManager getPackageManager() { return null; }
  public android.content.ComponentName startService(Intent i) { return null; }
  public android.content.ComponentName startForegroundService(Intent i) { return null; }
}
