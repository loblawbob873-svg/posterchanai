package android.content;

public abstract class Context {
  public static final int MODE_PRIVATE = 0;
  public abstract ContentResolver getContentResolver();
  public abstract SharedPreferences getSharedPreferences(String name, int mode);
  public Context getApplicationContext() { return this; }
  public java.io.File getFilesDir() { return null; }
}
