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
import place.poster.app.RunningNote;
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

  // The channel and the notification id live in RunningNote now — one item for every background
  // service this app runs. Keeping private copies here is how two of them came back.
  private static final String PREF_ON = "stay_connected";

  public static boolean running = false;

  private final Handler handler = new Handler(Looper.getMainLooper());
  private boolean audioCbOn = false;

  /**
   * THE FOLDER-SYNC HEARTBEAT, which is the only clock that runs while the screen is off.
   *
   * This service already keeps the process and its WebView alive — that was never the missing half.
   * The missing half was that nothing ASKED the page to sync: on Android there is no filesystem
   * watcher (SAF has no usable tree notification, so fs-android's `watch()` answers false), so the
   * client's only automatic trigger was a JS `setInterval`, and Android throttles timers in a hidden
   * WebView into uselessness. "Stay connected" was on, the process was up, and sync still stopped
   * the moment the screen went off.
   *
   * The clock lives here and the work stays in JS, the same split the music controls use. It only
   * ever emits.
   *
   * WHY THE PERIOD IS NOT A BATTERY DECISION. It looks like one and it is not: `shouldSync` on the
   * other side declines on battery, on a metered link, when the battery is low and inside the
   * minimum interval — which is exactly what the "only when plugged in" and "Wi-Fi only" switches
   * already mean. A tick arriving when those say no costs one policy check.
   *
   * IT IS AN ALARM, NOT A HANDLER, AND THAT DISTINCTION IS THE WHOLE FEATURE. `Handler.postDelayed`
   * schedules against `SystemClock.uptimeMillis()`, which STOPS ADVANCING IN DEEP SLEEP — and this
   * service holds no wake lock, because a foreground service keeps the process RESIDENT without
   * keeping the CPU AWAKE. A Handler therefore fires only when something else happens to wake the
   * phone, so the period stretches arbitrarily in exactly the state this exists for: in a pocket,
   * screen off, dozing. It would have looked like a fix and behaved like the bug.
   *
   * `setAndAllowWhileIdle` is the one that fires in Doze. Android rate-limits it to roughly once
   * every nine minutes per app, which is the real floor here and well under the period below.
   *
   * WHY THE PERIOD IS JUST OVER FIFTEEN MINUTES, and it is not a battery number. `shouldSync`
   * refuses when less than `minIntervalMs` (15 min) has passed and nothing is dirty — and on Android
   * nothing is ever dirty, because there is no watcher. So a ten-minute alarm aliases against that
   * floor and produces a TWENTY-minute effective period: sweep at 0, refused at 10, runs at 20. Just
   * above the floor means every alarm that fires does the work it woke up for.
   */
  private static final long SYNC_TICK_MS = 16 * 60 * 1000L;
  public static final String ACTION_SYNC_TICK = "place.poster.app.SYNC_TICK";
  private boolean ticking = false;

  private PendingIntent tickIntent() {
    // FLAG_IMMUTABLE is not optional — Android 12+ throws when the PendingIntent is built without it.
    return PendingIntent.getService(this, 0x5C12,
        new Intent(this, StayAwakeService.class).setAction(ACTION_SYNC_TICK),
        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
  }

  private void armTick() {
    android.app.AlarmManager am =
        (android.app.AlarmManager) getSystemService(Context.ALARM_SERVICE);
    if (am == null) return;
    long at = android.os.SystemClock.elapsedRealtime() + SYNC_TICK_MS;
    try {
      // ELAPSED_REALTIME_WAKEUP: counts through sleep, and wakes the device to deliver. The
      // non-WAKEUP variants would queue until something else woke the phone, which is the Handler's
      // failure wearing a different name.
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
        am.setAndAllowWhileIdle(android.app.AlarmManager.ELAPSED_REALTIME_WAKEUP, at, tickIntent());
      } else {
        am.set(android.app.AlarmManager.ELAPSED_REALTIME_WAKEUP, at, tickIntent());
      }
      // Counted only where the schedule actually took. Counting before the call would make an alarm
      // the OS refused indistinguishable from one it accepted and then never delivered — which are
      // opposite problems with opposite fixes, and the whole point of measuring this.
      try { place.poster.app.sync.FolderSyncPlugin.onAlarmArmed(); } catch (Throwable ignored) {}
    } catch (Throwable ignored) {}
  }
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
    if (ACTION_SYNC_TICK.equals(action)) {
      /* The alarm came back. Handled BEFORE the foreground block for the same reason DROP_STANDBY
       * is — it is a message, not a restart — and re-armed FIRST so a throw in the emit cannot end
       * the clock. `setAndAllowWhileIdle` is one-shot, so re-arming here is the repeat. */
      try { place.poster.app.sync.FolderSyncPlugin.onAlarmFired(); } catch (Throwable ignored) {}
      armTick();                      // re-armed FIRST: a throw below must not end the clock
      /* "Only when plugged in" / "Wi-Fi only", answered here rather than by waking the WebView to
       * be told the same thing. A pre-filter only — see FolderSyncPlugin.suppressed. */
      boolean skip = false;
      try { skip = place.poster.app.sync.FolderSyncPlugin.suppressed(this); } catch (Throwable ignored) {}
      if (!skip) {
        /* THE SWEEP ITSELF, IN JAVA, WHEN IT CAN BE. Chromium throttles a hidden page's JavaScript
         * however awake the processor is, so the tick below — which asks the WebView to sweep — is a
         * request the WebView may be in no position to honour. NativeRunner does the transfer itself
         * where the account key is on this device; it answers false for an account signed in through
         * Amber, where no key is here to sign a single upload with, and then the tick is all there
         * is. Both paths take the same per-folder lock, so they cannot overlap. */
        boolean native_ = false;
        try {
          native_ = place.poster.app.sync.NativeRunner.tick(this, "stay-connected");
        } catch (Throwable ignored) {}
        if (!native_) {
          try { place.poster.app.sync.FolderSyncPlugin.tick("stay-connected"); } catch (Throwable ignored) {}
        }
      }
      return START_STICKY;
    }
    if (ACTION_DROP_STANDBY.equals(action)) {
      // Handled BEFORE the foreground/re-arm block below: this is a message, not a restart, and
      // running the whole start path for it would re-open the very session it is asking us to close.
      closeStandbySession();
      return START_STICKY;
    }
    if (ACTION_STOP.equals(action)) {
      setWanted(this, false);
      closeStandbySession();   // the switch is off: stop being a media app the car can see
      dropNotification();
      stopSelf();
      return START_NOT_STICKY;
    }
    ensureChannel(this);
    /* BEFORE going foreground, not after — RunningNote composes the shared text from these flags,
     * so setting it afterwards makes the first notification of every start describe an app in which
     * this is not running. Put back in the catch below. */
    running = true;
    try {
      int type = Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE
          ? ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE : 0;
      ServiceCompat.startForeground(this, RunningNote.ID, RunningNote.build(this), type);
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
      /* ONCE, for the same reason the audio callback is: onStartCommand runs again on every restart
       * and on the STICKY relaunch, and a second posted Runnable would double the tick rate for the
       * life of the service — then treble it. */
      if (!ticking) {
        ticking = true;
        // Recorded so `armed` can be read honestly: this arm is a service start, not an alarm
        // that failed to come back. See FolderSyncPlugin's counter note.
        try { place.poster.app.sync.FolderSyncPlugin.onServiceStarted(); } catch (Throwable ignored) {}
        armTick();
      }
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

  /* The notification belongs to RunningNote now — ONE item in the shade however many of this app's
   * background services are up. Two permanent notifications from one app is the app's problem, not
   * the user's. */
  static void ensureChannel(Context ctx) {
    RunningNote.ensureChannel(ctx);
  }

  /**
   * Stand down from the shared notification.
   *
   * REMOVE would delete it out from under the signer if that is still up, leaving a running
   * foreground service with nothing in the shade. While anything else needs it we DETACH — the item
   * stays, it just stops being ours — and re-post it without us in the text.
   */
  private void dropNotification() {
    running = false;
    if (RunningNote.othersRunning(false)) {
      ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_DETACH);
      RunningNote.refresh(this);
    } else {
      ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE);
    }
  }

  @Override
  public void onDestroy() {
    running = false;
    /* The clock stops with the service. An alarm OUTLIVES the process — that is the point of it —
     * so leaving one armed would restart this service from a dead switch, and a STICKY relaunch
     * would then arm a second beside it. Cancelled by the same PendingIntent that set it. */
    try {
      android.app.AlarmManager am =
          (android.app.AlarmManager) getSystemService(Context.ALARM_SERVICE);
      if (am != null) am.cancel(tickIntent());
    } catch (Throwable ignored) {}
    ticking = false;
    /* Killed by the platform rather than switched off, so nothing has redrawn the shared
     * notification: take this half out of its text instead of leaving it naming a service that is
     * gone. If nothing else is up the item goes with the process anyway. */
    if (RunningNote.othersRunning(false)) RunningNote.refresh(this);
    closeStandbySession();
    if (audioCbOn) {
      AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
      if (am != null) { try { am.unregisterAudioDeviceCallback(deviceCb); } catch (Exception ignored) {} }
      audioCbOn = false;
    }
    super.onDestroy();
  }
}
