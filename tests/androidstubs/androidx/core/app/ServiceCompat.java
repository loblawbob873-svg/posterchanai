package androidx.core.app;

import android.app.Notification;
import android.app.Service;

public class ServiceCompat {
  public static final int STOP_FOREGROUND_REMOVE = 1;
  public static final int STOP_FOREGROUND_DETACH = 2;
  public static void startForeground(Service svc, int id, Notification n, int type) { }
  public static void stopForeground(Service svc, int flags) { }
}
