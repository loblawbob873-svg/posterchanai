package place.poster.app.music;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.drawable.BitmapDrawable;
import android.graphics.drawable.Drawable;
import android.media.AudioDeviceCallback;
import android.media.AudioDeviceInfo;
import android.media.AudioManager;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.SystemClock;
import android.view.KeyEvent;
import android.support.v4.media.MediaMetadataCompat;
import android.support.v4.media.session.MediaSessionCompat;
import android.support.v4.media.session.PlaybackStateCompat;

import androidx.core.app.NotificationCompat;
import androidx.core.app.ServiceCompat;
import androidx.core.content.ContextCompat;
import androidx.media.session.MediaButtonReceiver;

import place.poster.app.MainActivity;
import place.poster.app.R;

/**
 * The lock screen / notification-shade / Bluetooth transport for the Music player.
 *
 * WHY THIS EXISTS AT ALL. The player already speaks `navigator.mediaSession` (title, artwork,
 * play/pause/next/prev/seek handlers, position), and in a BROWSER that is the whole job — Chrome
 * turns it into the media notification. A WebView does not: the API is present, the handlers are
 * accepted, and nothing is ever shown, because the notification/lock-screen surface lives in
 * Chrome, not in the WebView. So in the APK the music played with no controls anywhere outside the
 * app — no lock screen, no shade, no headset buttons, no widget. This service is that missing half.
 *
 * WHERE THE AUDIO IS. Still in the WebView, and it has to be: a track is an encrypted blob that
 * only the client can decrypt (the key never leaves it), so there is no file for a native player to
 * open. This service therefore owns the SESSION, not the sound: it publishes what the WebView told
 * it is playing, and forwards every button press back to the WebView as a transport event. The
 * foreground notification is also what keeps the process alive with the screen off — the WebView's
 * renderer is otherwise a prime candidate for the low-memory killer, which is the difference between
 * music that survives a locked phone in a pocket and music that stops a minute in.
 *
 * AUDIO FOCUS IS DELIBERATELY NOT REQUESTED HERE. Requesting it from the service would be the third
 * of the classic four (session, notification, focus, noisy), but the WebView's own media stack may
 * already hold focus for the playing element, and a second request from the same app can steal it
 * from the first — at which point Chromium's media session PAUSES the element, i.e. the fix would
 * stop the music it exists to keep playing. That is not a trade to make blind on a box with no
 * device attached; it needs one measurement (start a track, watch whether another app's audio
 * ducks) and until then the safe half — pausing when the headphones are pulled out — is handled
 * below, which cannot break playback whether or not the WebView already does it.
 *
 * A PRESS IS NOT PLAYBACK, AND THIS SERVICE IS THE ONLY THING THAT CAN TELL THE DIFFERENCE. Every
 * button here ends in emit() — a call into the WebView — and the WebView is the half Android is free
 * to take away: its render process is killed under memory pressure (MainActivity recreates the
 * Activity when that happens, which reloads the page with an empty player), and a backgrounded
 * Activity can be destroyed outright while this foreground service keeps the process, the session
 * and the notification alive. Both leave a media notification whose every button lands in nothing,
 * for as long as the user stays out of the app — reported as "after a while in the car the
 * multimedia controls no longer work until I open the app again". Nothing is logged, because from
 * here the emit SUCCEEDED; there is simply nobody on the other end.
 *
 * So a press that is supposed to produce sound goes through press(), which checks for a RECEIPT:
 * the client answers EVERY transport event with an ack (see MusicPlayer._nativeInit — its handler
 * calls ack() synchronously, and it is a bare "I am here" precisely because a page that has just
 * reloaded holds no track and so has no state to push), so a press that has not been answered
 * within a second and a half was not performed. That is the only measurement available from this side, and it is
 * made rather than assumed. An unanswered press then tries to bring the app back, and — because a
 * background activity start is REFUSED SILENTLY on Android 10+, with no exception to catch — the
 * result of that is measured too, by looking for the same receipt afterwards. Whatever happens is
 * recorded in the diagnostic MusicPlugin.status() reports, and a player that never came back stops
 * pretending on the notification instead of showing a dead ▶.
 *
 * BLUETOOTH AUTOPLAY (opt-in, `autoplayBluetooth`) rides the same path. Getting into a car connects
 * an A2DP sink; most head units then send KEYCODE_MEDIA_PLAY, which arrives here as a media button
 * and is already handled — but plenty of them send nothing at all, which is why the audio-device
 * callback below exists. registerAudioDeviceCallback, NOT a BluetoothDevice broadcast: ACL_CONNECTED
 * and the A2DP connection-state broadcast both require the BLUETOOTH_CONNECT runtime permission on
 * Android 12+, which is a permission prompt about a Bluetooth feature for something the user asked
 * for in a music player, and the device TYPE — all this needs — is available without it.
 */
public class MusicService extends Service {

  /** From the plugin: the WebView's current state. */
  public static final String ACTION_UPDATE = "place.poster.app.MUSIC_UPDATE";
  /** From the notification / widget / a media button: what the user pressed. */
  public static final String ACTION_TOGGLE = "place.poster.app.MUSIC_TOGGLE";
  public static final String ACTION_PLAY = "place.poster.app.MUSIC_PLAY";
  public static final String ACTION_PAUSE = "place.poster.app.MUSIC_PAUSE";
  public static final String ACTION_NEXT = "place.poster.app.MUSIC_NEXT";
  public static final String ACTION_PREV = "place.poster.app.MUSIC_PREV";
  /** From the plugin: the player closed. JS already knows — do NOT tell it again (see ACTION_DISMISS). */
  public static final String ACTION_STOP = "place.poster.app.MUSIC_STOP";
  /** The user swiped the notification away. That one has to travel BACK to the player. */
  public static final String ACTION_DISMISS = "place.poster.app.MUSIC_DISMISS";

  public static final String EXTRA_TITLE = "title";
  public static final String EXTRA_ARTIST = "artist";
  public static final String EXTRA_PLAYING = "playing";
  public static final String EXTRA_POSITION = "position";
  public static final String EXTRA_DURATION = "duration";

  private static final String CHANNEL_ID = "music_playback";
  private static final int NOTIF_ID = 4243;

  /** Where the client's options live, so they survive the page (and the process) that set them. */
  public static final String PREFS = "pc_music";
  public static final String PREF_AUTOPLAY_BT = "autoplay_bt";

  /** How long a live client takes to answer a transport event: milliseconds, not seconds — the
   *  handler pushes its state back synchronously. 1500ms is slack for a busy main thread. */
  private static final long ANSWER_MS = 1500;
  /** …and how long the app gets to come back once we have asked it to. A cold start is a WebView, a
   *  page load and a signer; a warm one is a resume. */
  private static final long REVIVE_MS = 20000;
  /** One car connection reports several devices arriving; this is how long they count as one. */
  private static final long AUTOPLAY_GAP_MS = 10000;

  /** Transport bridge back to the Capacitor plugin (which forwards to JS as `musicTransport`). */
  public interface Listener { void onTransport(String action, double value); }
  private static volatile Listener listener;
  public static void setListener(Listener l) { listener = l; }

  public static volatile MusicService INSTANCE;

  private MediaSessionCompat session;
  private Bitmap art;
  private boolean foreground = false;

  private String title = "";
  private String artist = "PosterChan";
  private boolean playing = false;
  private long positionMs = 0;
  private long durationMs = 0;

  /** What the widget last drew, so a once-a-second position push doesn't repaint it 3600 times an hour. */
  private String widgetKey = null;

  private final Handler handler = new Handler(Looper.getMainLooper());

  /** elapsedRealtime of the last state push from the WebView — the ONE proof it is still there. */
  private volatile long lastWebAt = 0;
  /** Set when a press went unanswered and the app did not come back; cleared by the next push. */
  private volatile boolean webGone = false;

  private volatile boolean autoplayBt = false;
  private long lastAutoplayAt = 0;
  /** registerAudioDeviceCallback fires immediately with everything ALREADY connected. That is not a
   *  car door opening — it is this service starting — and treating it as one would autoplay on every
   *  start made while a speaker happens to be paired. */
  private boolean firstDeviceSweep = true;

  /* Counters + the last thing that happened, for MusicPlugin.status(). There is no device here and
   * this failure REPORTS SUCCESS from every side (the notification is up, the emit returned, and
   * nothing plays), so the only way to tell the possible causes apart is to have the phone say which
   * one it measured. */
  static volatile int btConnects = 0, btAutoplays = 0, unanswered = 0, revived = 0;
  static volatile String note = "";

  static void emit(String action, double value) {
    Listener l = listener;
    if (l != null) l.onTransport(action, value);
  }

  boolean isPlaying() { return playing; }

  /** Milliseconds since the client last said anything, or -1 if it never has. */
  long webSilenceMs() { return lastWebAt == 0 ? -1 : SystemClock.elapsedRealtime() - lastWebAt; }
  boolean webGone() { return webGone; }
  boolean autoplayBluetooth() { return autoplayBt; }

  /**
   * The client's options. Written through SharedPreferences rather than held in the service, because
   * the service is the thing that outlives the page: a WebView that is reloaded (or a renderer that
   * died) comes back with no memory of what the user chose, and a car connecting to a service that
   * has been up for an hour must still honour a switch flipped before any of it.
   */
  public static void setAutoplayBluetooth(Context ctx, boolean on) {
    try {
      ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
         .edit().putBoolean(PREF_AUTOPLAY_BT, on).apply();
    } catch (Exception ignored) {}
    MusicService svc = INSTANCE;
    if (svc != null) svc.autoplayBt = on;
  }

  public static boolean autoplayBluetooth(Context ctx) {
    try {
      return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(PREF_AUTOPLAY_BT, false);
    } catch (Exception e) {
      return false;   // opt-in: an unreadable preference is "the user never asked for this"
    }
  }

  private static void note(String s) { MusicService.note = s; }

  /** Debounce for the BACKGROUND entry point below. Static, because the whole point of that path is
   *  that there is no instance to hold it. */
  private static long lastBgAutoplayAt = 0;
  /** Set when a background autoplay could not start the service, so Details can say so. */
  static volatile int btRefused = 0;

  /**
   * A Bluetooth sink connected while THIS SERVICE WAS NOT RUNNING — the case the feature was missing.
   *
   * `deviceCb` above only exists once MusicService has been created, and MusicService is created by
   * something PLAYING. So autoplay required the exact action it is meant to replace: you had to open
   * the player and start a song to arm the thing that starts a song for you. Reported as "I still
   * have to manually play the song", with Details reading `media controls: not running`.
   *
   * The listener that fixes it cannot live here, so it lives in the service that IS alive with the
   * app closed — StayAwakeService, the "stay connected" foreground service — which calls this. All
   * the POLICY stays in this class on purpose: one debounce, one opt-in check, one place where the
   * counters move. StayAwakeService only contributes the one thing it has that we do not: being
   * awake.
   *
   * No new permission. An AudioDeviceInfo TYPE is readable without BLUETOOTH_CONNECT, which is the
   * same reason `deviceCb` uses this API rather than the ACL/A2DP broadcasts.
   */
  public static void onBluetoothSinkConnected(Context ctx) {
    btConnects++;
    long now = SystemClock.elapsedRealtime();
    // One connection reports the A2DP sink, sometimes an SCO one, sometimes both twice — and if the
    // service IS up, its own callback is debouncing the same event on its own clock.
    if (now - lastBgAutoplayAt < AUTOPLAY_GAP_MS) return;
    lastBgAutoplayAt = now;
    if (!autoplayBluetooth(ctx)) { note("bluetooth connected (app closed) — autoplay is off"); return; }
    MusicService svc = INSTANCE;
    if (svc != null) {
      // The service is up after all: its own deviceCb has this, and firing here as well would be a
      // second press for one car door.
      note("bluetooth connected — the player was already running");
      return;
    }
    btAutoplays++;
    /* ACTION_PLAY into a service with no state lands in the `!foreground` branch of onStartCommand,
     * which is the COLD path that already exists for a head unit's media button: it hands a live
     * page the press, or `revive()`s the app when there is none, and gets out of the way. Nothing
     * new to maintain — a car that sends KEYCODE_MEDIA_PLAY and a car that sends nothing now arrive
     * at the same code.
     *
     * startService, not startForegroundService: that cold branch deliberately never calls
     * startForeground (it has nothing to publish), and a startForegroundService that does not is
     * killed with a crash five seconds later. A plain start is allowed here because the caller is a
     * running foreground service, which keeps the app out of the background for this purpose. If the
     * platform refuses anyway, SAY SO in the counter rather than failing the way this whole feature
     * has been failing — silently.
     */
    Intent i = new Intent(ctx, MusicService.class).setAction(ACTION_PLAY);
    try {
      ctx.startService(i);
      note("bluetooth connected (app closed) — asked the player to start");
    } catch (Throwable t) {
      btRefused++;
      note("bluetooth connected (app closed) — Android refused the start: " + t.getClass().getSimpleName());
    }
  }

  /**
   * A press that is supposed to produce SOUND, sent with a receipt check.
   *
   * NOT `command()` — that name already belongs to the PendingIntent builder for the notification's
   * buttons, three lines of which are `command(ACTION_NEXT)` and friends. Java does not consider the
   * return type part of a signature, so a second `command(String)` is a duplicate method and the
   * whole module stops compiling: the CI APK never builds while `sync.sh` ships the client half
   * regardless, i.e. a JS side talking to a native side that does not exist on any phone.
   *
   * emit() cannot fail — it is a call into a listener that is still registered long after the page
   * behind it has gone — so the only evidence that anything happened is the ack the client sends in
   * reply. If none arrives, nobody performed the press.
   *
   * NOT used for the pause that follows headphones being unplugged: that one is a safety measure,
   * and an unanswered one means the audio is already gone with the WebView that was playing it —
   * launching the app to tell it to stop would be a car stereo opening an app for no reason.
   */
  private void press(final String action) {
    final long before = lastWebAt;
    emit(action, 0);
    handler.postDelayed(() -> {
      if (lastWebAt != before) return;         // answered — the player is alive and did the work
      unanswered++;
      revive(action);
    }, ANSWER_MS);
  }

  /**
   * A press that only ever STOPS something, with the same receipt check and no wake-up.
   *
   * Pause needs the check as much as play does — arguably more, because `playing` is only ever
   * changed by the client, so a WebView that vanished mid-track leaves the service believing a track
   * is playing FOREVER. The notification's one transport button is drawn from that belief, so it
   * stays a ⏸ that takes the bare-emit branch on every press: the most-pressed control on the whole
   * surface, permanently dead, and never even reaching the state that says so. What it must NOT do
   * is `revive()` — dragging the app to the foreground in someone's car to tell it to stop playing
   * something that is already silent is worse than the silence.
   */
  private void hush(final String action) {
    final long before = lastWebAt;
    emit(action, 0);
    handler.postDelayed(() -> {
      if (lastWebAt != before) return;
      unanswered++;
      note(action + ": the player is not answering");
      markGone();                              // …which also drops `playing`, freeing the toggle
    }, ANSWER_MS);
  }

  /**
   * "I am still here" — the client's answer to a transport event, and nothing else.
   *
   * Separate from apply() on purpose. A page that has just reloaded holds no track, so it has no
   * state to push and `_nativePush` refuses to send one (pushing with no track would raise a
   * notification about nothing). Without this, a LIVE client that received a press and handled it
   * correctly is indistinguishable from a dead one: the service counts it unanswered, wakes an app
   * that is already awake, and eight seconds later writes "the player stopped responding" across a
   * notification the user is looking at.
   */
  void ack() {
    lastWebAt = SystemClock.elapsedRealtime();
    if (webGone) { webGone = false; try { publish(); } catch (Exception ignored) {} }
  }

  /** A widget button, routed through the same receipt check every other surface uses. */
  void fromWidget(String action) {
    if (ACTION_NEXT.equals(action)) press("next");
    else if (ACTION_PREV.equals(action)) press("prev");
    else if (playing) hush("pause");
    else press("play");
  }

  /**
   * Nobody answered. Try to bring the app back and hand it the press.
   *
   * The action travels as the SAME launch extra the home-screen widget uses, so it is consumed once
   * (MusicPlugin.consumeLaunchAction) and performed by the client on the way up — one mechanism, not
   * a second one that could disagree with it.
   *
   * A background activity start is REFUSED SILENTLY from Android 10 on: no exception, nothing but a
   * line in the system log. So the outcome is measured the same way the press was — by waiting for
   * the receipt — and what is measured is what status() reports and what the notification says.
   */
  private void revive(final String action) {
    revived++;
    final long before = lastWebAt;
    try {
      startActivity(new Intent(this, MainActivity.class)
          .setAction(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
          .putExtra(MusicPlugin.EXTRA_LAUNCH_ACTION, action)
          .putExtra(MusicPlugin.EXTRA_LAUNCH_AT, System.currentTimeMillis())
          .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP));
    } catch (Exception e) {
      note(action + ": the player is not answering and the app could not be started (" + e + ")");
      markGone();
      return;
    }
    note(action + ": the player is not answering — asked the app to come back");
    handler.postDelayed(() -> {
      if (lastWebAt != before) { note(action + ": the player came back and did it"); return; }
      note(action + ": the player never came back (Android refuses to open an app from the background)");
      markGone();
    }, REVIVE_MS);
  }

  /** Stop offering a transport that controls nothing — say so on the notification instead. */
  private void markGone() {
    if (webGone) return;
    webGone = true;
    playing = false;      // whatever it was doing, it is not doing it now
    try { publish(); } catch (Exception ignored) {}
  }

  /**
   * A Bluetooth audio device arrived — in practice, a car.
   *
   * Only ever a PLAY, only when the user asked for it, and only when this service is already up:
   * with the player closed there is no session, no notification and nothing decrypted, so there is
   * nothing here to resume and a phone that starts playing music on its own in someone's car is the
   * behaviour people uninstall an app over.
   */
  private final AudioDeviceCallback deviceCb = new AudioDeviceCallback() {
    @Override public void onAudioDevicesAdded(AudioDeviceInfo[] added) {
      if (firstDeviceSweep) { firstDeviceSweep = false; return; }
      if (added == null) return;
      boolean bt = false;
      for (AudioDeviceInfo d : added) {
        if (d == null || !d.isSink()) continue;
        int t = d.getType();
        if (t == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP) bt = true;
        else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S
                 && (t == AudioDeviceInfo.TYPE_BLE_HEADSET || t == AudioDeviceInfo.TYPE_BLE_SPEAKER)) bt = true;
      }
      if (!bt) return;
      btConnects++;
      long now = SystemClock.elapsedRealtime();
      // One connection reports the A2DP sink, sometimes an SCO one, sometimes both twice.
      if (now - lastAutoplayAt < AUTOPLAY_GAP_MS) return;
      lastAutoplayAt = now;
      if (!autoplayBt) { note("bluetooth connected — autoplay is off"); return; }
      if (playing) { note("bluetooth connected — already playing"); return; }
      btAutoplays++;
      note("bluetooth connected — playing");
      press("play");
    }
  };

  /**
   * Repaint every placed widget from the state we hold, bypassing the change check below — a widget
   * can be ADDED mid-song, and its first onUpdate has nothing to draw unless we resend what is
   * already playing.
   */
  void renderWidget() { MusicWidget.render(this, title, artist, playing, true); }

  /**
   * Headphones pulled / Bluetooth gone. Every music player on the phone pauses here; one that keeps
   * going blasts the track out of the handset speaker in whatever room the user is standing in.
   */
  private final BroadcastReceiver noisy = new BroadcastReceiver() {
    @Override public void onReceive(Context c, Intent i) {
      if (playing) emit("pause", 0);
    }
  };

  @Override public IBinder onBind(Intent intent) { return null; }

  @Override
  public void onCreate() {
    super.onCreate();
    INSTANCE = this;

    NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && nm != null) {
      NotificationChannel ch = new NotificationChannel(
          CHANNEL_ID, "Music playback", NotificationManager.IMPORTANCE_LOW);
      ch.setShowBadge(false);
      ch.setSound(null, null);        // a media notification that chimes on every track change
      nm.createNotificationChannel(ch);
    }

    session = new MediaSessionCompat(this, "PosterChanMusic");
    session.setCallback(new MediaSessionCompat.Callback() {
      /* The car's own buttons land HERE, not in onStartCommand — the platform routes a media button
       * to the session it belongs to. So these three are the presses that most need the receipt
       * check: a steering wheel is exactly where nobody can see that nothing happened. */
      @Override public void onPlay() { press("play"); }
      @Override public void onPause() { hush("pause"); }
      @Override public void onSkipToNext() { press("next"); }
      @Override public void onSkipToPrevious() { press("prev"); }
      @Override public void onStop() { emit("stop", 0); }
      @Override public void onSeekTo(long pos) { emit("seekTo", pos / 1000.0); }
      @Override public void onFastForward() { emit("seekBy", 10); }
      @Override public void onRewind() { emit("seekBy", -10); }
    });
    art = launcherIcon();
    // ContextCompat, not registerReceiver(): from Android 14 a context-registered receiver must say
    // whether it is exported, and an app targeting 34+ that doesn't gets a SecurityException instead
    // of a receiver. NOT_EXPORTED is right — becoming-noisy is a protected system broadcast, which
    // the platform still delivers to a receiver marked private.
    ContextCompat.registerReceiver(this, noisy,
        new IntentFilter(AudioManager.ACTION_AUDIO_BECOMING_NOISY), ContextCompat.RECEIVER_NOT_EXPORTED);

    autoplayBt = autoplayBluetooth(this);
    AudioManager am = (AudioManager) getSystemService(AUDIO_SERVICE);
    if (am != null) {
      // On the main looper on purpose: everything the callback touches (playing, the notification,
      // the emit) belongs to that thread, and a car connecting is not a hot path.
      try { am.registerAudioDeviceCallback(deviceCb, handler); } catch (Exception ignored) {}
    }
  }

  @Override
  public int onStartCommand(Intent intent, int flags, int startId) {
    // START_NOT_STICKY everywhere: the sound is in the WebView, so a service Android restarts on its
    // own would publish a notification whose buttons control nothing at all.
    if (intent == null) return START_NOT_STICKY;
    String action = intent.getAction();

    /* The two ends of a stop, kept apart on purpose. A dismissal starts OUT here and has to reach the
     * player; a plugin stop arrives BECAUSE the player already stopped, and echoing it back would
     * make the two chase each other — JS closes → we emit stop → JS closes again → … */
    if (ACTION_DISMISS.equals(action)) { emit("stop", 0); shutdown(); return START_NOT_STICKY; }
    if (ACTION_STOP.equals(action)) { shutdown(); return START_NOT_STICKY; }

    if (ACTION_UPDATE.equals(action)) {
      // The FIRST update — the one that started us. It must go foreground within ~5 seconds of that
      // start or Android kills the whole app, and publish() is what does it.
      apply(intent.getStringExtra(EXTRA_TITLE), intent.getStringExtra(EXTRA_ARTIST),
            intent.getBooleanExtra(EXTRA_PLAYING, false),
            intent.getDoubleExtra(EXTRA_POSITION, 0), intent.getDoubleExtra(EXTRA_DURATION, 0));
      return START_NOT_STICKY;
    }

    /* NOT foreground means this instance was created by the arriving intent — in practice a media
     * button routed here by the platform with the player long gone (getting into the car after the
     * app was closed). There is no state to publish and nothing to command, and a service started
     * with startForegroundService that never calls startForeground is killed with a crash five
     * seconds later, so the honest move is to ask the app to come back and get out of the way. */
    if (!foreground) {
      String want = coldPress(action, intent);
      if (want != null) {
        /* A LIVE CLIENT IS STILL A CLIENT, even though this service instance has no state. `listener`
         * is non-null only while a page is loaded with the transport handler armed, so handing it the
         * press is both cheaper and less rude than launching an activity at somebody — it will start
         * playing and its first push raises a properly-stateful service a moment later. Reviving on
         * top of that would foreground the app for a press it is already performing. */
        if (listener != null) emit(want, 0);
        else revive(want);
      }
      stopSelf(startId);
      return START_NOT_STICKY;
    }

    if (ACTION_TOGGLE.equals(action)) { if (playing) hush("pause"); else press("play"); }
    else if (ACTION_PLAY.equals(action)) press("play");
    else if (ACTION_PAUSE.equals(action)) hush("pause");
    else if (ACTION_NEXT.equals(action)) press("next");
    else if (ACTION_PREV.equals(action)) press("prev");
    else MediaButtonReceiver.handleIntent(session, intent);   // a headset / steering-wheel button

    return START_NOT_STICKY;
  }

  /**
   * What a press means when this service has no state — the cold case above.
   *
   * The KeyEvent is read rather than assumed for two reasons. A media button broadcast delivers the
   * DOWN and the UP of one press as two separate intents, so taking every one at face value would
   * wake the app twice per press; and a car's ⏭ is a media button too, which as a blanket "play"
   * would start the library at track one when the driver asked for the next song. A pause or a stop
   * with nothing playing is exactly what it says it is — nothing — and must not open anything.
   */
  private static String coldPress(String action, Intent intent) {
    if (ACTION_PAUSE.equals(action) || ACTION_STOP.equals(action)) return null;
    if (ACTION_NEXT.equals(action)) return "next";
    if (ACTION_PREV.equals(action)) return "prev";
    if (Intent.ACTION_MEDIA_BUTTON.equals(action)) {
      KeyEvent ev = null;
      try { ev = intent.getParcelableExtra(Intent.EXTRA_KEY_EVENT); } catch (Exception ignored) {}
      if (ev == null || ev.getAction() != KeyEvent.ACTION_DOWN) return null;
      switch (ev.getKeyCode()) {
        case KeyEvent.KEYCODE_MEDIA_NEXT: return "next";
        case KeyEvent.KEYCODE_MEDIA_PREVIOUS: return "prev";
        case KeyEvent.KEYCODE_MEDIA_PAUSE:
        case KeyEvent.KEYCODE_MEDIA_STOP: return null;
        default: return "play";
      }
    }
    return "play";
  }

  private static String str(String s, String dflt) { return (s == null || s.isEmpty()) ? dflt : s; }

  /**
   * The state push from the WebView, taken DIRECTLY rather than through startForegroundService().
   *
   * This runs about once a second for the length of a track, and most of those seconds are with the
   * screen off — which is precisely when Android 12+ refuses a foreground-service start from the
   * background (ForegroundServiceStartNotAllowedException). Going through the start path would have
   * meant every update after the phone locked being thrown away, freezing the lock screen on
   * whatever was showing when it locked: the wrong title after a track change, and a play button on
   * a paused song. Same process, so this is a plain method call; the Intent path below stays for the
   * one start that does need it (the first, made while the app is on screen).
   */
  public void apply(String newTitle, String newArtist, boolean isPlaying, double posSec, double durSec) {
    // THE RECEIPT. Every push is also proof the WebView is still there and still answering, which is
    // the one thing this service cannot otherwise find out (see press()). The client keeps pushing
    // while PAUSED for exactly this reason — a paused player is the state the failure happens in.
    lastWebAt = SystemClock.elapsedRealtime();
    webGone = false;
    title = str(newTitle, "Track");
    artist = str(newArtist, "PosterChan");
    playing = isPlaying;
    positionMs = (long) (posSec * 1000);
    durationMs = (long) (durSec * 1000);
    publish();
  }

  /**
   * The launcher icon as a Bitmap, for the lock screen's artwork.
   *
   * NOT BitmapFactory.decodeResource: since API 26 the launcher icon is an ADAPTIVE icon — an XML
   * drawable — and decodeResource returns null for those. It would have worked on the emulator image
   * everyone tests on least (pre-Oreo) and quietly shipped a lock screen with no artwork on every
   * phone in use. Drawing the drawable covers both shapes.
   */
  private Bitmap launcherIcon() {
    try {
      Drawable d = ContextCompat.getDrawable(this, R.mipmap.ic_launcher);
      if (d == null) return null;
      if (d instanceof BitmapDrawable) return ((BitmapDrawable) d).getBitmap();
      int w = d.getIntrinsicWidth(), h = d.getIntrinsicHeight();
      if (w <= 0 || h <= 0) { w = 192; h = 192; }     // adaptive icons can report no intrinsic size
      Bitmap b = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
      Canvas c = new Canvas(b);
      d.setBounds(0, 0, w, h);
      d.draw(c);
      return b;
    } catch (Exception e) {
      return null;   // artwork is decoration; a media session without it still works
    }
  }

  /** Push the current state to the media session, the notification and the home-screen widget. */
  private void publish() {
    MediaMetadataCompat.Builder md = new MediaMetadataCompat.Builder()
        .putString(MediaMetadataCompat.METADATA_KEY_TITLE, title)
        .putString(MediaMetadataCompat.METADATA_KEY_ARTIST, artist)
        .putString(MediaMetadataCompat.METADATA_KEY_ALBUM, "Library");
    // Only when it is real. A duration of 0 makes a lock screen draw a scrubber it can never fill,
    // and -1 (the "unknown" convention) is what tells it to draw none — the honest answer while a
    // track is still loading.
    md.putLong(MediaMetadataCompat.METADATA_KEY_DURATION, durationMs > 0 ? durationMs : -1);
    if (art != null) md.putBitmap(MediaMetadataCompat.METADATA_KEY_ALBUM_ART, art);
    session.setMetadata(md.build());

    /* Position + speed, not position alone: with a speed the system INTERPOLATES the progress bar
     * between our updates. That matters more than it looks — the WebView's timers are throttled once
     * the app is backgrounded, which is exactly when the lock screen is the only thing on show, and a
     * bar fed only by those updates would crawl or stall while the music played on. */
    long actions = PlaybackStateCompat.ACTION_PLAY
        | PlaybackStateCompat.ACTION_PAUSE
        | PlaybackStateCompat.ACTION_PLAY_PAUSE
        | PlaybackStateCompat.ACTION_SKIP_TO_NEXT
        | PlaybackStateCompat.ACTION_SKIP_TO_PREVIOUS
        | PlaybackStateCompat.ACTION_STOP
        | PlaybackStateCompat.ACTION_SEEK_TO
        | PlaybackStateCompat.ACTION_FAST_FORWARD
        | PlaybackStateCompat.ACTION_REWIND;
    session.setPlaybackState(new PlaybackStateCompat.Builder()
        .setActions(actions)
        .setState(playing ? PlaybackStateCompat.STATE_PLAYING : PlaybackStateCompat.STATE_PAUSED,
                  positionMs, playing ? 1f : 0f, SystemClock.elapsedRealtime())
        .build());
    if (!session.isActive()) session.setActive(true);

    Notification n = notification();
    if (!foreground) {
      ServiceCompat.startForeground(this, NOTIF_ID, n,
          Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q
              ? ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK : 0);
      foreground = true;
    } else {
      NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
      if (nm != null) nm.notify(NOTIF_ID, n);
    }

    // The widget carries no clock, so it only ever changes when the track or the play state does.
    String key = title + "\0" + artist + "\0" + playing;
    if (!key.equals(widgetKey)) {
      widgetKey = key;
      MusicWidget.render(this, title, artist, playing, true);
    }
  }

  private Notification notification() {
    PendingIntent open = PendingIntent.getActivity(this, 0,
        new Intent(this, MainActivity.class).setAction(Intent.ACTION_MAIN)
            .addCategory(Intent.CATEGORY_LAUNCHER)
            .setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP | Intent.FLAG_ACTIVITY_NEW_TASK),
        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);

    NotificationCompat.Builder b = new NotificationCompat.Builder(this, CHANNEL_ID)
        .setSmallIcon(R.drawable.ic_music_note)
        .setContentTitle(title)
        // A transport that controls nothing must not look like one that does. When a press went
        // unanswered AND the app could not be brought back, this line is the only place the user can
        // be told why the buttons stopped working — and the tap target is already the app.
        .setContentText(webGone ? "Tap to reopen — the player stopped responding" : artist)
        .setLargeIcon(art)
        .setContentIntent(open)
        .setDeleteIntent(command(ACTION_DISMISS))    // swiped away = stop, not "playing invisibly"
        .setShowWhen(false)
        .setOnlyAlertOnce(true)
        .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)   // readable on a LOCKED screen — the point
        .setPriority(NotificationCompat.PRIORITY_LOW)
        .addAction(R.drawable.ic_media_prev, "Previous", command(ACTION_PREV))
        .addAction(playing ? R.drawable.ic_media_pause : R.drawable.ic_media_play,
                   playing ? "Pause" : "Play", command(ACTION_TOGGLE))
        .addAction(R.drawable.ic_media_next, "Next", command(ACTION_NEXT))
        .setStyle(new androidx.media.app.NotificationCompat.MediaStyle()
            .setMediaSession(session.getSessionToken())
            .setShowActionsInCompactView(0, 1, 2));
    return b.build();
  }

  /** A button on the notification: back into onStartCommand, which forwards it to the WebView. */
  private PendingIntent command(String action) {
    Intent i = new Intent(this, MusicService.class).setAction(action);
    return PendingIntent.getService(this, action.hashCode(), i,
        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
  }

  /**
   * The user swiped the app off the recents list. That destroys the Activity and with it the WebView
   * — i.e. the audio has already stopped — so the notification must go too. Without this the shade
   * keeps a media notification whose every button lands in a process that has nothing playing.
   */
  @Override public void onTaskRemoved(Intent rootIntent) { shutdown(); }

  void shutdown() {
    handler.removeCallbacksAndMessages(null);   // a press made a second before the close must not relaunch the app
    playing = false;
    // Forget what the widget was last drawn with, or a later session that happens to start on the
    // SAME track sees an unchanged key and skips the repaint — leaving the widget on its idle face
    // while the music plays.
    widgetKey = null;
    MusicWidget.render(this, null, null, false, false);
    if (session != null) { session.setActive(false); }
    ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE);
    foreground = false;
    stopSelf();
  }

  @Override
  public void onDestroy() {
    handler.removeCallbacksAndMessages(null);   // a receipt check firing after we are gone
    try { unregisterReceiver(noisy); } catch (Exception ignored) {}
    try {
      AudioManager am = (AudioManager) getSystemService(AUDIO_SERVICE);
      if (am != null) am.unregisterAudioDeviceCallback(deviceCb);
    } catch (Exception ignored) {}
    if (session != null) { session.setActive(false); session.release(); session = null; }
    MusicWidget.render(this, null, null, false, false);
    if (INSTANCE == this) INSTANCE = null;
    super.onDestroy();
  }
}
