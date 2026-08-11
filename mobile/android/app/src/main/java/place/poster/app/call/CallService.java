package place.poster.app.call;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.IBinder;

import androidx.core.app.NotificationCompat;
import androidx.core.app.ServiceCompat;

import place.poster.app.MainActivity;
import place.poster.app.R;

/**
 * The foreground service that lets a call survive leaving the app.
 *
 * WHY THIS EXISTS, and it is not the same reason as MusicService's. Music needed a native half
 * because the WebView will not draw a media notification. A CALL needs one because of a platform
 * RULE: since Android 11 an app in the background cannot capture the microphone or the camera at
 * all, unless a foreground service with the matching type is running. So without this service,
 * pressing Home mid-call does not risk anything — it silences your microphone, immediately and
 * silently. The other party hears nothing, your own UI looks completely normal, and there is no
 * error anywhere. That is the worst shape a bug can have.
 *
 * It also does what the music service's notification does as a side effect: a process hosting a
 * foreground service is not a candidate for the cached-process freezer or the low-memory killer, so
 * the WebRTC PeerConnection (which lives in the WebView, not here) keeps running with the screen off
 * instead of being frozen a few seconds after the screen locks.
 *
 * WHERE THE CALL IS. Still entirely in the WebView: the PeerConnection, the media tracks, the Nostr
 * signalling and the key are all JS. This service holds no media and no state beyond what to draw.
 * It owns PERMISSION and PRIORITY, nothing else — which is why hanging up from the notification is
 * an event sent BACK to JS to perform, rather than anything torn down here.
 *
 * TYPES. `microphone` always; `camera` as well for a video call, because the same Android 11 rule
 * applies to the camera and a video call that keeps its mic and loses its picture is not better. On
 * Android 14+ each declared type also needs its own permission (FOREGROUND_SERVICE_MICROPHONE /
 * _CAMERA) or the start throws — see the manifest.
 */
public class CallService extends Service {

  public static final String ACTION_START = "place.poster.app.CALL_START";
  public static final String ACTION_UPDATE = "place.poster.app.CALL_UPDATE";
  public static final String ACTION_STOP = "place.poster.app.CALL_STOP";
  /** The user pressed "Hang up" ON THE NOTIFICATION. Travels back to JS, which owns the call. */
  public static final String ACTION_HANGUP = "place.poster.app.CALL_HANGUP";

  public static final String EXTRA_VIDEO = "video";
  public static final String EXTRA_NAME = "name";
  public static final String EXTRA_STATE = "state";

  private static final String CHANNEL = "pcai_ongoing_calls";
  private static final int NOTIF_ID = 4711;

  /** So the plugin can refresh a RUNNING service without another startForegroundService. */
  public static CallService INSTANCE = null;

  public interface Listener { void onCallAction(String action); }
  private static Listener listener = null;
  public static void setListener(Listener l) { listener = l; }

  private boolean video = false;
  private String name = "";
  private String state = "";

  @Override
  public IBinder onBind(Intent intent) { return null; }

  @Override
  public int onStartCommand(Intent intent, int flags, int startId) {
    String action = intent != null ? intent.getAction() : null;
    if (ACTION_HANGUP.equals(action)) {
      // JS owns the call — it has to send `bye` to the peer, stop the tracks and close the
      // PeerConnection. Tearing anything down here would end the notification while the call itself
      // carried on in the WebView, which is the one outcome worse than no button at all.
      if (listener != null) listener.onCallAction("hangup");
      return START_NOT_STICKY;
    }
    if (ACTION_STOP.equals(action)) {
      INSTANCE = null;
      ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE);
      stopSelf();
      return START_NOT_STICKY;
    }

    if (intent != null) {
      if (intent.hasExtra(EXTRA_VIDEO)) video = intent.getBooleanExtra(EXTRA_VIDEO, false);
      if (intent.hasExtra(EXTRA_NAME)) name = String.valueOf(intent.getStringExtra(EXTRA_NAME));
      if (intent.hasExtra(EXTRA_STATE)) state = String.valueOf(intent.getStringExtra(EXTRA_STATE));
    }
    INSTANCE = this;
    ensureChannel(this);
    startInForeground();
    // NOT sticky. A restarted service would put a "call in progress" notification on screen for a
    // call that died with the process, and its Hang up button would reach nothing.
    return START_NOT_STICKY;
  }

  private void startInForeground() {
    int types = 0;
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
      types = ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
      if (video) types |= ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA;
    }
    try {
      ServiceCompat.startForeground(this, NOTIF_ID, build(), types);
    } catch (Throwable t) {
      // A refused start must not take the app down WITH the call still running. Losing the service
      // costs the background microphone; throwing here costs everything.
      stopSelf();
    }
  }

  /** Refresh the notification in place (mute state, ringing → connected). */
  public void refresh(boolean vid, String who, String st) {
    this.video = vid; this.name = who; this.state = st;
    NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
    if (nm != null) nm.notify(NOTIF_ID, build());
  }

  private Notification build() {
    Intent open = new Intent(this, MainActivity.class)
        .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
    // FLAG_IMMUTABLE on every PendingIntent: Android 12 throws when the notification is built
    // without it. Same trap the music notification documents.
    int f = PendingIntent.FLAG_UPDATE_CURRENT
        | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0);
    PendingIntent tap = PendingIntent.getActivity(this, 0, open, f);
    PendingIntent hang = PendingIntent.getService(this, 1,
        new Intent(this, CallService.class).setAction(ACTION_HANGUP), f);

    String title = (name != null && name.length() > 0) ? name : "PosterChan";
    String text = ("ringing".equals(state) ? "Incoming call"
                 : "connecting".equals(state) ? "Connecting…"
                 : video ? "Video call in progress" : "Call in progress");

    return new NotificationCompat.Builder(this, CHANNEL)
        .setContentTitle(title)
        .setContentText(text)
        .setSmallIcon(R.mipmap.ic_launcher)
        .setCategory(NotificationCompat.CATEGORY_CALL)
        .setPriority(NotificationCompat.PRIORITY_HIGH)
        // Ongoing + no auto-cancel: this is the way back INTO the call, so it must not be swipeable
        // away while the call is still up.
        .setOngoing(true)
        .setAutoCancel(false)
        .setShowWhen(true)
        .setUsesChronometer(true)
        .setContentIntent(tap)
        .addAction(android.R.drawable.ic_menu_close_clear_cancel, "Hang up", hang)
        .build();
  }

  static void ensureChannel(Context ctx) {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
    NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
    if (nm == null || nm.getNotificationChannel(CHANNEL) != null) return;
    // Its OWN channel, separate from the incoming-call channel PushEventService uses: this one is
    // silent (you are already in the call — a sound would be absurd) while that one rings. Android
    // only lets a person silence notifications per channel, so sharing one would mean silencing the
    // ringer to stop the in-call notification making noise.
    NotificationChannel ch = new NotificationChannel(CHANNEL, "Ongoing calls",
        NotificationManager.IMPORTANCE_LOW);
    ch.setDescription("Shown while you are on a call, so it keeps running when you leave the app.");
    ch.setShowBadge(false);
    ch.setSound(null, null);
    nm.createNotificationChannel(ch);
  }

  @Override
  public void onDestroy() {
    if (INSTANCE == this) INSTANCE = null;
    super.onDestroy();
  }
}
