package place.poster.app.push;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

import androidx.core.app.NotificationCompat;
import androidx.core.app.ServiceCompat;

import place.poster.app.MainActivity;
import place.poster.app.R;

/**
 * "Stay connected" — the persistent notification other messaging apps show.
 *
 * WHAT IT IS FOR, and it is a fallback, not the plan. The intended path for reaching a closed app is
 * PUSH: the server watches its own relay and posts to a UnifiedPush endpoint, the distributor wakes
 * the phone, PushEventService draws the notification, and this app's process need not be running at
 * all. That costs nothing and is what should be used.
 *
 * It has one hard requirement: a distributor app (ntfy, Sunup, …) must be installed. Plenty of people
 * will not install one, and for them a closed PosterChan receives NOTHING — no DMs, no calls, no
 * mentions — which is indistinguishable from the feature being broken. This service is the answer for
 * exactly that case: a foreground service keeps the process off the cached-process freezer and the
 * low-memory killer, so the WebView keeps its relay socket and keeps raising notifications itself
 * (through PushPlugin.notify → PushEventService.show, the same builder a push uses).
 *
 * SO IT IS OFF BY DEFAULT AND THE COST IS STATED. A live WebSocket and an unfrozen renderer is real
 * battery; the alternative for these users is no notifications at all, and that is a trade only they
 * can make. The notification says which one they picked and turns it off in one tap.
 *
 * `specialUse` is the declared type, not `dataSync`: Android 15 caps dataSync at six hours in any
 * twenty-four, which for a "stay connected" service means it silently stops working most of the day.
 */
public class StayAwakeService extends Service {

  public static final String ACTION_START = "place.poster.app.STAY_START";
  public static final String ACTION_STOP = "place.poster.app.STAY_STOP";

  private static final String CHANNEL = "pcai_stay_connected";
  private static final int NOTIF_ID = 4712;
  private static final String PREF_ON = "stay_connected";

  public static boolean running = false;

  @Override
  public IBinder onBind(Intent intent) { return null; }

  /** Remembered so the boot/relaunch path can put it back without asking the web layer first. */
  public static boolean wanted(Context ctx) {
    return ctx.getSharedPreferences(PushEventService.PREFS, Context.MODE_PRIVATE)
              .getBoolean(PREF_ON, false);
  }

  public static void setWanted(Context ctx, boolean on) {
    ctx.getSharedPreferences(PushEventService.PREFS, Context.MODE_PRIVATE)
       .edit().putBoolean(PREF_ON, on).apply();
  }

  @Override
  public int onStartCommand(Intent intent, int flags, int startId) {
    String action = intent != null ? intent.getAction() : null;
    if (ACTION_STOP.equals(action)) {
      running = false;
      setWanted(this, false);
      ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE);
      stopSelf();
      return START_NOT_STICKY;
    }
    ensureChannel(this);
    try {
      int type = Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE
          ? ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE : 0;
      ServiceCompat.startForeground(this, NOTIF_ID, build(), type);
      running = true;
      setWanted(this, true);
    } catch (Throwable t) {
      running = false;
      stopSelf();
      return START_NOT_STICKY;
    }
    // STICKY, unlike the call service: a call that died with the process is over, but "stay
    // connected" is a standing preference — if Android kills us for memory, coming back is the whole
    // point of having asked for it.
    return START_STICKY;
  }

  private Notification build() {
    int f = PendingIntent.FLAG_UPDATE_CURRENT
        | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0);
    PendingIntent tap = PendingIntent.getActivity(this, 0,
        new Intent(this, MainActivity.class)
            .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP), f);
    PendingIntent off = PendingIntent.getService(this, 1,
        new Intent(this, StayAwakeService.class).setAction(ACTION_STOP), f);

    return new NotificationCompat.Builder(this, CHANNEL)
        .setContentTitle("PosterChan is staying connected")
        .setContentText("So messages and calls reach you. Costs battery.")
        .setSmallIcon(R.mipmap.ic_launcher)
        // MINIMUM priority and no badge: this is a receipt for a setting, not news. A person who has
        // opted into a permanent notification should be able to forget it is there.
        .setPriority(NotificationCompat.PRIORITY_MIN)
        .setOngoing(true)
        .setShowWhen(false)
        .setContentIntent(tap)
        .addAction(0, "Turn off", off)
        .build();
  }

  static void ensureChannel(Context ctx) {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
    NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
    if (nm == null || nm.getNotificationChannel(CHANNEL) != null) return;
    NotificationChannel ch = new NotificationChannel(CHANNEL, "Staying connected",
        NotificationManager.IMPORTANCE_MIN);
    ch.setDescription("The permanent notification Android requires while the app keeps its "
                    + "connection open in the background.");
    ch.setShowBadge(false);
    ch.setSound(null, null);
    nm.createNotificationChannel(ch);
  }

  @Override
  public void onDestroy() {
    running = false;
    super.onDestroy();
  }
}
