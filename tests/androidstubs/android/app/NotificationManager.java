package android.app;

public class NotificationManager {
  public static final int IMPORTANCE_MIN = 1;
  public static final int IMPORTANCE_LOW = 2;
  public static final int IMPORTANCE_DEFAULT = 3;
  public static final int IMPORTANCE_HIGH = 4;
  public void notify(int id, Notification n) { }
  public void cancel(int id) { }
  public void createNotificationChannel(NotificationChannel channel) { }
  public void deleteNotificationChannel(String channelId) { }
  public NotificationChannel getNotificationChannel(String channelId) { return null; }
}
