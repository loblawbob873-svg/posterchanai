package android.app;

import android.content.Context;
import android.content.Intent;

public class PendingIntent {
  public static final int FLAG_UPDATE_CURRENT = 0x08000000;
  public static final int FLAG_IMMUTABLE = 0x04000000;
  public static PendingIntent getBroadcast(Context ctx, int req, Intent i, int flags) { return null; }
  public static PendingIntent getService(Context ctx, int req, Intent i, int flags) { return null; }
  public static PendingIntent getActivity(Context ctx, int req, Intent i, int flags) { return null; }
}
