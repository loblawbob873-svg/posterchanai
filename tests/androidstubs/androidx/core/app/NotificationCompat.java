package androidx.core.app;

import android.app.Notification;
import android.app.PendingIntent;
import android.content.Context;

public class NotificationCompat {
  public static final int PRIORITY_MIN = -2;
  public static final int PRIORITY_LOW = -1;
  public static final int PRIORITY_DEFAULT = 0;
  public static final int PRIORITY_HIGH = 1;

  public static class Builder {
    public Builder(Context ctx, String channelId) { }
    public Builder setContentTitle(CharSequence t) { return this; }
    public Builder setContentText(CharSequence t) { return this; }
    public Builder setSubText(CharSequence t) { return this; }
    public Builder setSmallIcon(int icon) { return this; }
    public Builder setPriority(int pri) { return this; }
    public Builder setOngoing(boolean ongoing) { return this; }
    public Builder setShowWhen(boolean show) { return this; }
    public Builder setAutoCancel(boolean autoCancel) { return this; }
    public Builder setContentIntent(PendingIntent pi) { return this; }
    public Builder addAction(int icon, CharSequence title, PendingIntent pi) { return this; }
    public Notification build() { return null; }
  }
}
