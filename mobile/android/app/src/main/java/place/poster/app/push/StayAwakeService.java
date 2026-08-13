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
import android.media.AudioDeviceCallback;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.support.v4.media.MediaMetadataCompat;
import android.support.v4.media.session.MediaSessionCompat;
import android.support.v4.media.session.PlaybackStateCompat;

import androidx.core.app.NotificationCompat;
import androidx.core.app.ServiceCompat;

import place.poster.app.MainActivity;
import place.poster.app.R;
import place.poster.app.music.MusicService;

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
  /** MusicService took over with a real session — drop the standby one. */
  public static final String ACTION_DROP_STANDBY = "place.poster.app.STAY_DROP_STANDBY";

  private static final String CHANNEL = "pcai_stay_connected";
  private static final int NOTIF_ID = 4712;
  private static final String PREF_ON = "stay_connected";

  public static boolean running = false;

  private final Handler handler = new Handler(Looper.getMainLooper());
  private boolean audioCbOn = false;
  /** registerAudioDeviceCallback fires immediately with everything ALREADY connected. That is this
   *  service starting — at boot, most often — not a car door, and treating it as one would autoplay
   *  every time the phone reboots near a paired speaker. Same rule as MusicService's own sweep. */
  private boolean firstDeviceSweep = true;

  /**
   * BLUETOOTH AUTOPLAY WITH THE APP CLOSED, which is the only state a car actually finds it in.
   *
   * MusicService has had this listener for a while, and it could never fire for the case it was
   * written for: that service is created by something PLAYING, so arming autoplay required the very
   * action autoplay exists to replace. Details said `media controls: not running` and the answer was
   * "I still have to manually play the song".
   *
   * This service is the one that is up with the app closed — it is a foreground service, BootReceiver
   * restarts it, and it already owns a permanent notification — so the listener belongs here and the
   * decision stays in MusicService.onBluetoothSinkConnected. Nothing new is spent: no permission (a
   * device TYPE is readable without BLUETOOTH_CONNECT, which is why this API was chosen over the
   * ACL/A2DP broadcasts), no second notification, no boot changes.
   *
   * THE COST IS THE COUPLING, and it has to be said out loud in the UI: autoplay now depends on
   * "stay connected" being on. It is off by default, so for anyone who has not turned it on this
   * changes nothing — which is exactly the silent no-op the feature has been all along, and the
   * switch's description is where that gets fixed, not here.
   */
  private final AudioDeviceCallback deviceCb = new AudioDeviceCallback() {
    @Override public void onAudioDevicesAdded(AudioDeviceInfo[] added) {
      /* RECORDED BEFORE ANY TEST, including the first sweep. `btConnects` counts Bluetooth matches,
       * so a zero there cannot distinguish "this never fired" from "it fired and nothing matched" —
       * measured in a real car as `bluetooth 0 connect/s` with the listener confirmed armed, which
       * left both possibilities open and no way to choose but to guess. */
      StringBuilder types = new StringBuilder();
      if (added != null) {
        for (AudioDeviceInfo d : added) {
          if (d == null) continue;
          if (types.length() > 0) types.append(',');
          types.append(d.getType()).append(d.isSink() ? "" : "(src)");
        }
      }
      MusicService.onAudioDevicesSeen(types.length() == 0 ? "none" : types.toString());
      if (firstDeviceSweep) { firstDeviceSweep = false; return; }
      if (added == null) return;
      for (AudioDeviceInfo d : added) {
        if (d == null || !d.isSink()) continue;
        int t = d.getType();
        boolean bt = t == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP
            || (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S
                && (t == AudioDeviceInfo.TYPE_BLE_HEADSET || t == AudioDeviceInfo.TYPE_BLE_SPEAKER));
        if (bt) { MusicService.onBluetoothSinkConnected(StayAwakeService.this); return; }
      }
    }
  };

  /**
   * A STANDBY MEDIA SESSION — the thing every other media player has and this app did not.
   *
   * Android routes a car's PLAY button to an ACTIVE MediaSession, and a head unit reads its "now
   * playing" line from that session's metadata. This app only ever created a session once a track
   * was already playing, so before the first song there was no session at all: the car showed no
   * song info, and its play button had nowhere to go. Both reported, and both are this.
   *
   * Spotify and the rest "just work" because they keep a session alive whether or not sound is
   * coming out. This is that. It is held by the service that is up with the app closed, it declares
   * PLAY/PAUSE/NEXT/PREVIOUS so the car offers those controls, and every press is handed to the same
   * cold path a media button already used (`MusicService` ACTION_PLAY → revive the page and perform
   * it). It publishes no false state: STATE_PAUSED with no position is exactly what it is.
   *
   * It gets out of the way the moment there is a real one. `MusicService` builds its own session
   * when a track plays, and two active sessions in one app means the car can pick the wrong one —
   * so this releases as soon as that exists.
   */
  private MediaSessionCompat standby = null;

  private void openStandbySession() {
    if (standby != null || MusicService.INSTANCE != null) return;
    if (!MusicService.autoplayBluetooth(this)) return;   // opt-in: no switch, no session
    try {
      standby = new MediaSessionCompat(this, "PosterChanStandby");
      standby.setCallback(new MediaSessionCompat.Callback() {
        @Override public void onPlay() { handOff(MusicService.ACTION_PLAY); }
        @Override public void onPause() { /* nothing is playing; a pause here is a no-op, not a wake */ }
        @Override public void onSkipToNext() { handOff(MusicService.ACTION_NEXT); }
        @Override public void onSkipToPrevious() { handOff(MusicService.ACTION_PREV); }
      });
      /* The actions are what the car DRAWS. Declaring none leaves a head unit with a media app it
       * can see and cannot operate, which is indistinguishable from the app being absent. */
      standby.setPlaybackState(new PlaybackStateCompat.Builder()
          .setActions(PlaybackStateCompat.ACTION_PLAY | PlaybackStateCompat.ACTION_PLAY_PAUSE
                    | PlaybackStateCompat.ACTION_SKIP_TO_NEXT
                    | PlaybackStateCompat.ACTION_SKIP_TO_PREVIOUS)
          .setState(PlaybackStateCompat.STATE_PAUSED, 0, 0f)
          .build());
      // Something for the head unit to show. Deliberately not a song title — no song is loaded, and
      // naming one would put a lie on the dashboard.
      standby.setMetadata(new MediaMetadataCompat.Builder()
          .putString(MediaMetadataCompat.METADATA_KEY_TITLE, "PosterChan")
          .putString(MediaMetadataCompat.METADATA_KEY_ARTIST, "Ready to play")
          .build());
      standby.setActive(true);
      MusicService.setStandbySession(true);
    } catch (Throwable t) {
      standby = null;
      MusicService.setStandbySession(false);
    }
  }

  /** Hand a press to the existing cold path rather than growing a second one. */
  private void handOff(String action) {
    try {
      startService(new Intent(this, MusicService.class).setAction(action));
    } catch (Throwable ignored) {}
  }

  void closeStandbySession() {
    if (standby == null) return;
    try { standby.setActive(false); standby.release(); } catch (Throwable ignored) {}
    standby = null;
    MusicService.setStandbySession(false);
  }

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
    if (ACTION_DROP_STANDBY.equals(action)) {
      // Handled BEFORE the foreground/re-arm block below: this is a message, not a restart, and
      // running the whole start path for it would re-open the very session it is asking us to close.
      closeStandbySession();
      return START_STICKY;
    }
    if (ACTION_STOP.equals(action)) {
      running = false;
      setWanted(this, false);
      closeStandbySession();   // the switch is off: stop being a media app the car can see
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
      /* Only AFTER going foreground, and only once: onStartCommand runs again on every restart and
       * on the STICKY relaunch, and registering a second time would deliver every connection twice
       * (the debounce would absorb it, which is precisely why a duplicate registration would go
       * unnoticed until it did not). */
      if (!audioCbOn) {
        AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
        if (am != null) {
          // Main looper: a car connecting is not a hot path, and MusicService's own callback runs
          // there too, so the two can never race over the same debounce.
          // …and say whether it took. A registration that threw left `stayConnected: true` beside a
          // listener that does not exist — the panel then reports the service, not the listener.
          try { am.registerAudioDeviceCallback(deviceCb, handler); audioCbOn = true; }
          catch (Exception ignored) {}
          MusicService.setListening(audioCbOn);
        }
      }
      /* The session the car actually talks to — OUTSIDE the audioCbOn guard, because it is a
       * different thing with a different lifetime: the listener is registered once for the life of
       * the service, while the session must be reopened whenever a real one has come and gone
       * (MusicService releases its own on close, and openStandbySession is a no-op while one
       * exists). Without it the head unit sees no media app at all: nothing to display, and a PLAY
       * button with nowhere to route. */
      openStandbySession();
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
    closeStandbySession();
    if (audioCbOn) {
      AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
      if (am != null) { try { am.unregisterAudioDeviceCallback(deviceCb); } catch (Exception ignored) {} }
      audioCbOn = false;
    }
    super.onDestroy();
  }
}
