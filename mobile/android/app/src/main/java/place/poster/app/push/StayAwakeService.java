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
   * THE FOLDER-SYNC CLOCK USED TO LIVE HERE, AND THAT WAS THE BUG.
   *
   * It was put here because this is the service that is up with the app closed — true, and not the
   * same as "the only one that can be". "Stay connected" is OFF BY DEFAULT and is described,
   * correctly, as a fallback for receiving DMs and calls where no push distributor is installed. So
   * on every phone that had never touched that switch there was NO CLOCK AT ALL, and background
   * folder sync could not run however good the sweep engine got: the alarm that fires in Doze, the
   * wake lock, its renewal, resumeTimers and finally an entire native sweep were all downstream of a
   * tick nothing emitted. Reported, correctly, as syncing stopping shortly after the screen goes
   * off — on two devices, across several rounds of "fixed".
   *
   * A FEATURE HAS TO ASK FOR WHAT IT NEEDS ITSELF. That is the same lesson the background signer
   * cost (it could not sign, because the only thing that ever stored a key was an unrelated switch
   * in another part of settings). The clock is {@code sync.SyncClock} now — armed by the folder-sync
   * plugin whenever this account actually syncs a folder, landing in {@code sync.SyncTickReceiver},
   * sweeping inside {@code sync.SyncService}. There is exactly ONE of it: a second one left here
   * would double every wake-up on the phones that do have this switch on, which is the opposite of
   * what the switch is for.
   *
   * The action below survives only to swallow an alarm armed by a build older than this one.
   * PendingIntents outlive an app update, so without it a stale tick falls through to the start path
   * and restarts a service the user may have turned off.
   */
  public static final String ACTION_SYNC_TICK = "place.poster.app.SYNC_TICK";

  private void cancelLegacyTick() {
    try {
      android.app.AlarmManager am =
          (android.app.AlarmManager) getSystemService(Context.ALARM_SERVICE);
      if (am == null) return;
      int f = PendingIntent.FLAG_UPDATE_CURRENT
          | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0);
      am.cancel(PendingIntent.getService(this, 0x5C12,
          new Intent(this, StayAwakeService.class).setAction(ACTION_SYNC_TICK), f));
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
      /* A tick armed by a build older than this one, arriving after the update. Cancel it and stop:
       * the clock is SyncClock now (see the note above), and doing the work here as well would mean
       * two alarms and two sweeps on any phone that has this switch on. Handled BEFORE the
       * foreground block because it is a message, not a restart — falling through would start a
       * service the user may have turned off. */
      cancelLegacyTick();
      if (!running) stopSelf();   // started only to receive this: do not linger un-foregrounded
      return running ? START_STICKY : START_NOT_STICKY;
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
      // Belt and braces for an install coming from the build where the folder-sync alarm lived here:
      // its PendingIntent survived the update, and this is the first moment we are certainly running.
      cancelLegacyTick();
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
    if (RunningNote.othersRunning(RunningNote.STAY)) {
      ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_DETACH);
      RunningNote.refresh(this);
    } else {
      ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE);
    }
  }

  @Override
  public void onDestroy() {
    running = false;
    /* The folder-sync alarm is NOT cancelled here any more, and that is the point of moving it: it
     * belongs to folder sync, which keeps running whether or not this switch is on. Only the legacy
     * one — armed by a build where it did live here — is cleared, and it is cleared unconditionally
     * because nothing else will ever cancel it. */
    cancelLegacyTick();
    /* Killed by the platform rather than switched off, so nothing has redrawn the shared
     * notification: take this half out of its text instead of leaving it naming a service that is
     * gone. If nothing else is up the item goes with the process anyway. */
    if (RunningNote.othersRunning(RunningNote.STAY)) RunningNote.refresh(this);
    closeStandbySession();
    if (audioCbOn) {
      AudioManager am = (AudioManager) getSystemService(Context.AUDIO_SERVICE);
      if (am != null) { try { am.unregisterAudioDeviceCallback(deviceCb); } catch (Exception ignored) {} }
      audioCbOn = false;
    }
    super.onDestroy();
  }
}
