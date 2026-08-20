package androidx.core.app;

import android.app.Notification;
import android.app.PendingIntent;
import android.content.Context;

public class NotificationCompat {
  public static final int PRIORITY_MIN = -2;
  public static final int PRIORITY_LOW = -1;
  public static final int PRIORITY_DEFAULT = 0;
  public static final int PRIORITY_HIGH = 1;
  public static final String CATEGORY_MESSAGE = "msg";
  public static final String CATEGORY_CALL = "call";

  /** An expandable notification body. Used by the SMS notifier so a long text is readable. */
  public static class BigTextStyle extends Style {
    public BigTextStyle bigText(CharSequence t) { return this; }
  }

  public static class Style { }

  /** A notification action, and (for a reply box) the RemoteInput attached to it. */
  public static class Action {
    public static class Builder {
      public Builder(int icon, CharSequence title, PendingIntent pi) { }
      public Builder addRemoteInput(RemoteInput r) { return this; }
      public Builder setAllowGeneratedReplies(boolean b) { return this; }
      public Action build() { return null; }
    }
  }

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
    public Builder addAction(Action a) { return this; }
    public Builder setStyle(Style s) { return this; }
    public Builder setCategory(String c) { return this; }
    public Builder setWhen(long when) { return this; }
    /** The ringing call's way onto a locked screen — a background activity start is refused there. */
    public Builder setFullScreenIntent(PendingIntent pi, boolean highPriority) { return this; }
    public Notification build() { return null; }
  }
}
