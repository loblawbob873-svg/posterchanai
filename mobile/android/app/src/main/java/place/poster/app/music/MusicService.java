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
import android.media.AudioManager;
import android.os.Build;
import android.os.IBinder;
import android.os.SystemClock;
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

  static void emit(String action, double value) {
    Listener l = listener;
    if (l != null) l.onTransport(action, value);
  }

  boolean isPlaying() { return playing; }

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
      @Override public void onPlay() { emit("play", 0); }
      @Override public void onPause() { emit("pause", 0); }
      @Override public void onSkipToNext() { emit("next", 0); }
      @Override public void onSkipToPrevious() { emit("prev", 0); }
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

    if (ACTION_TOGGLE.equals(action)) emit(playing ? "pause" : "play", 0);
    else if (ACTION_PLAY.equals(action)) emit("play", 0);
    else if (ACTION_PAUSE.equals(action)) emit("pause", 0);
    else if (ACTION_NEXT.equals(action)) emit("next", 0);
    else if (ACTION_PREV.equals(action)) emit("prev", 0);
    else MediaButtonReceiver.handleIntent(session, intent);   // a headset / steering-wheel button

    return START_NOT_STICKY;
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
    String key = title + " " + artist + " " + playing;
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
        .setContentText(artist)
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
    try { unregisterReceiver(noisy); } catch (Exception ignored) {}
    if (session != null) { session.setActive(false); session.release(); session = null; }
    MusicWidget.render(this, null, null, false, false);
    if (INSTANCE == this) INSTANCE = null;
    super.onDestroy();
  }
}
