package android.app;

public class AlarmManager {
  public static final int ELAPSED_REALTIME_WAKEUP = 2;
  public static final int RTC_WAKEUP = 0;
  public void set(int type, long triggerAtMillis, PendingIntent op) { }
  public void setAndAllowWhileIdle(int type, long triggerAtMillis, PendingIntent op) { }
  public void setExact(int type, long triggerAtMillis, PendingIntent op) { }
  public void setExactAndAllowWhileIdle(int type, long triggerAtMillis, PendingIntent op) { }
  public boolean canScheduleExactAlarms() { return false; }
  public void cancel(PendingIntent op) { }
}
