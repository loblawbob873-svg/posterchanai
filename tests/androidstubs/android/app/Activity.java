package android.app;

import android.content.Context;

public class Activity extends Context {
  public static final int RESULT_OK = -1;
  public static final int RESULT_CANCELED = 0;
  public void runOnUiThread(Runnable r) { }
  public android.content.ContentResolver getContentResolver() { return null; }
  public android.content.SharedPreferences getSharedPreferences(String n, int m) { return null; }
}
