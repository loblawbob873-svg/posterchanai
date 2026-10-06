package android.os;

public class PowerManager {
  public static final int PARTIAL_WAKE_LOCK = 1;
  public static final String ACTION_POWER_SAVE_MODE_CHANGED = "android.os.action.POWER_SAVE_MODE_CHANGED";
  public boolean isPowerSaveMode() { return false; }
  public WakeLock newWakeLock(int levelAndFlags, String tag) { return null; }
  public boolean isIgnoringBatteryOptimizations(String pkg) { return false; }
  /** Is the display on. Real since API 20; the pre-20 spelling was isScreenOn(). */
  public boolean isInteractive() { return false; }
  public class WakeLock {
    public void acquire() { }
    public void acquire(long timeout) { }
    public void release() { }
    public boolean isHeld() { return false; }
    public void setReferenceCounted(boolean value) { }
  }
}
