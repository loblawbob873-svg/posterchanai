package place.poster.app.screenshare;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.media.MediaRecorder;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.Build;
import android.os.IBinder;
import android.util.DisplayMetrics;
import android.util.Log;
import android.view.WindowManager;

import androidx.core.app.NotificationCompat;
import androidx.core.app.ServiceCompat;
import androidx.core.content.ContextCompat;

import com.pedro.common.ConnectChecker;
import com.pedro.encoder.input.sources.audio.MicrophoneSource;
import com.pedro.encoder.input.sources.video.NoVideoSource;
import com.pedro.encoder.input.sources.video.ScreenSource;
import com.pedro.library.rtmp.RtmpStream;

import place.poster.app.MainActivity;

/**
 * Screen sharing for the live-stream feature.
 *
 * Android's WebView implements no getDisplayMedia at all, so the screen can never reach the WebRTC/WHIP
 * path the camera uses. Instead we capture natively (MediaProjection), encode H264 + AAC, and push RTMP to
 * the SAME MediaMTX ingest OBS publishes to — the app just hands us `rtmp://host:1935/<token>?key=<api_key>`
 * from /api/streams/ingest. Everything downstream (HLS remux, the kind-30311 announce) is unchanged.
 *
 * The ordering below is forced by the platform and is the whole ballgame: the user must consent FIRST, then
 * the foreground service must be running, and only THEN may getMediaProjection() be called. Reverse any of
 * it and Android 10+ throws SecurityException.
 */
public class ScreenShareService extends Service implements ConnectChecker {

  public static final String ACTION_START = "place.poster.app.SCREEN_START";
  public static final String ACTION_STOP = "place.poster.app.SCREEN_STOP";
  public static final String EXTRA_RESULT_CODE = "resultCode";
  public static final String EXTRA_RESULT_DATA = "resultData";
  public static final String EXTRA_URL = "url";
  public static final String EXTRA_MUTED = "muted";

  private static final String TAG = "ScreenShare";
  private static final String CHANNEL_ID = "screen_share";
  private static final int NOTIF_ID = 4242;

  /** Status bridge back to the Capacitor plugin (which forwards to JS as `screenShareStatus`). */
  public interface Listener { void onEvent(String event, String message); }
  private static volatile Listener listener;
  public static void setListener(Listener l) { listener = l; }
  private static void emit(String event, String message) {
    Listener l = listener;
    if (l != null) l.onEvent(event, message);
  }

  public static volatile ScreenShareService INSTANCE;

  private RtmpStream stream;
  private MicrophoneSource mic;
  private MediaProjection projection;
  private boolean prepared = false;
  private boolean muted = false;

  private static final int FPS = 30;
  private static final int V_BITRATE = 4_000_000;
  private static final int SAMPLE_RATE = 44100;
  private static final int A_BITRATE = 128_000;
  private static final int MAX_LONG_SIDE = 1920;

  // The user stopped the share from the system UI (the "Stop sharing" chip), or the screen locked — on
  // Android 15 QPR1 that also stops the projection. Without this the capture dies but the app still thinks
  // it's live, and viewers freeze on the last frame.
  private final MediaProjection.Callback projectionCallback = new MediaProjection.Callback() {
    @Override public void onStop() {
      emit("stopped", "screen sharing stopped");
      shutdown();
    }
  };

  @Override public IBinder onBind(Intent intent) { return null; }

  @Override
  public void onCreate() {
    super.onCreate();
    INSTANCE = this;
    NotificationManager nm = (NotificationManager) getSystemService(NOTIFICATION_SERVICE);
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && nm != null) {
      nm.createNotificationChannel(new NotificationChannel(
          CHANNEL_ID, "Screen sharing", NotificationManager.IMPORTANCE_LOW));
    }
    // Start with NoVideoSource: the real ScreenSource needs a MediaProjection, which we may not create until
    // we're in the foreground (see onStartCommand). The source is swapped in once we have it.
    mic = new MicrophoneSource(MediaRecorder.AudioSource.DEFAULT);
    stream = new RtmpStream(getApplicationContext(), this, new NoVideoSource(), mic);
    // MediaProjection only produces a frame when the screen CHANGES. On a static screen the encoder starves
    // and the RTMP feed stalls (no HLS segments, viewers see nothing) — force a floor of 15fps.
    stream.getGlInterface().setForceRender(true, 15);

    int[] size = encodeSize();
    try {
      prepared = stream.prepareVideo(size[0], size[1], V_BITRATE, FPS, 2, size[2])
          && stream.prepareAudio(SAMPLE_RATE, true, A_BITRATE, true, true);
    } catch (IllegalArgumentException e) {
      prepared = false;
    }
    stream.getStreamClient().setReTries(10);
  }

  /**
   * Encoder dimensions + rotation, as RootEncoder wants them: always the LANDSCAPE-ordered pair, with
   * rotation=90 asking for a portrait picture. Scaled so the long side is at most 1920 and both sides stay
   * even (odd dimensions are rejected by some hardware H264 encoders).
   */
  private int[] encodeSize() {
    DisplayMetrics dm = new DisplayMetrics();
    WindowManager wm = (WindowManager) getSystemService(Context.WINDOW_SERVICE);
    int w = 1280, h = 720;
    if (wm != null) {
      wm.getDefaultDisplay().getRealMetrics(dm);
      w = dm.widthPixels;
      h = dm.heightPixels;
    }
    boolean portrait = h >= w;
    int lo = portrait ? w : h;          // short side
    int hi = portrait ? h : w;          // long side
    if (hi > MAX_LONG_SIDE) {
      lo = (int) ((long) lo * MAX_LONG_SIDE / hi);
      hi = MAX_LONG_SIDE;
    }
    lo = Math.max(2, lo & ~1);
    hi = Math.max(2, hi & ~1);
    return new int[]{ hi, lo, portrait ? 90 : 0 };   // width, height (landscape order), rotation
  }

  @Override
  public int onStartCommand(Intent intent, int flags, int startId) {
    if (intent == null) return START_NOT_STICKY;
    if (ACTION_STOP.equals(intent.getAction())) { shutdown(); return START_NOT_STICKY; }
    if (!ACTION_START.equals(intent.getAction())) return START_NOT_STICKY;

    // We were launched with startForegroundService(), which gives us ~5 seconds to call startForeground() or
    // Android kills the process (ForegroundServiceDidNotStartInTimeException). EVERY path out of here must
    // therefore go foreground first — including the failures below, which used to just return and take the
    // whole app down with them instead of showing "screen share failed". The failure notification claims no
    // service type: type mediaProjection may only be claimed once the user's consent is actually in hand.
    Intent consent = intent.getParcelableExtra(EXTRA_RESULT_DATA);
    String url = intent.getStringExtra(EXTRA_URL);
    if (!prepared || consent == null || url == null) {
      goForeground(0);
      fail(!prepared ? "this device's encoder rejected the screen settings" : "missing screen-capture consent");
      return START_NOT_STICKY;
    }

    // Foreground FIRST — getMediaProjection() throws SecurityException otherwise (Android 10+).
    goForeground(serviceTypes());
    try {
      MediaProjectionManager mpm = (MediaProjectionManager)
          getApplicationContext().getSystemService(Context.MEDIA_PROJECTION_SERVICE);
      if (mpm == null) throw new IllegalStateException("no MediaProjectionManager");
      projection = mpm.getMediaProjection(intent.getIntExtra(EXTRA_RESULT_CODE, 0), consent);
      if (projection == null) throw new IllegalStateException("screen capture was not granted");
      // Registering a MediaProjection.Callback is mandatory on Android 14+ — createVirtualDisplay() throws
      // IllegalStateException without one.
      stream.changeVideoSource(new ScreenSource(getApplicationContext(), projection, projectionCallback, null));
      // Apply the mute BEFORE going on air. Muting after startStream would put the mic live for the length of
      // the round-trip — real audio from someone who already told us they were muted (they muted the camera
      // stream, then switched to the screen).
      setMuted(intent.getBooleanExtra(EXTRA_MUTED, false));
      stream.startStream(url);
      emit("starting", "");
    } catch (Exception e) {
      Log.e(TAG, "screen share failed to start", e);
      fail(String.valueOf(e.getMessage()));
    }
    return START_NOT_STICKY;
  }

  private int serviceTypes() {
    if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return 0;
    int types = ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION;
    // Android 14 throws if we claim the microphone type without RECORD_AUDIO actually granted. The plugin
    // asks for it before we ever get here; if the user refused, stream the screen silently rather than crash.
    if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
        == PackageManager.PERMISSION_GRANTED) {
      types |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
    }
    return types;
  }

  private void goForeground(int types) {
    int flags = PendingIntent.FLAG_IMMUTABLE;
    // Tapping the notification reopens the app; the Stop action ends the capture outright. This is the ONLY
    // way out if Android has killed the WebView while we kept capturing — without it the user's screen keeps
    // being broadcast and the app has no UI left to stop it.
    Intent open = new Intent(this, MainActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
    PendingIntent openPi = PendingIntent.getActivity(this, 0, open, flags);
    Intent stop = new Intent(this, ScreenShareService.class).setAction(ACTION_STOP);
    PendingIntent stopPi = PendingIntent.getService(this, 1, stop, flags);

    Notification n = new NotificationCompat.Builder(this, CHANNEL_ID)
        .setSmallIcon(android.R.drawable.presence_video_online)
        .setContentTitle("PosterChan — you're live")
        .setContentText("Your screen is being streamed")
        .setContentIntent(openPi)
        .addAction(0, "Stop", stopPi)
        .setOngoing(true)
        .setSilent(true)
        .build();
    ServiceCompat.startForeground(this, NOTIF_ID, n, types);
  }

  public boolean isStreaming() { return stream != null && stream.isStreaming(); }

  /** Mute/unmute the voiceover mid-broadcast. The capture keeps running; the mic just stops being encoded. */
  public void setMuted(boolean value) {
    if (mic == null) return;
    if (value) mic.mute(); else mic.unMute();
    muted = value;
  }

  public boolean isMuted() { return muted; }

  private void fail(String reason) {
    emit("error", reason);
    shutdown();
  }

  /** Tear the capture down. Safe to call twice (Stop from JS can race the system's own stop). */
  private void shutdown() {
    try { if (stream != null && stream.isStreaming()) stream.stopStream(); } catch (Exception ignored) {}
    if (projection != null) { try { projection.stop(); } catch (Exception ignored) {} projection = null; }
    ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE);
    stopSelf();
  }

  @Override
  public void onDestroy() {
    super.onDestroy();
    try { if (stream != null && stream.isStreaming()) stream.stopStream(); } catch (Exception ignored) {}
    if (stream != null) { try { stream.release(); } catch (Exception ignored) {} }
    if (projection != null) { try { projection.stop(); } catch (Exception ignored) {} projection = null; }
    INSTANCE = null;
  }

  // ---- ConnectChecker. RootEncoder dispatches all of these on the main thread. ----
  @Override public void onConnectionStarted(String url) { emit("connecting", ""); }
  @Override public void onConnectionSuccess() { emit("connected", ""); }

  @Override
  public void onConnectionFailed(String reason) {
    // RootEncoder does not reconnect on its own. Retrying reuses the existing MediaProjection, so the user is
    // NOT asked to consent again; only when the retries are exhausted do we give up and tell JS to end.
    if (stream != null && stream.getStreamClient().reTry(5000, reason, null)) {
      emit("reconnecting", reason);
    } else {
      fail(reason);
    }
  }

  @Override public void onDisconnect() { emit("disconnected", ""); }
  @Override public void onAuthError() { fail("the server rejected this stream key"); }
  @Override public void onAuthSuccess() { }
  @Override public void onNewBitrate(long bitrate) { }
}
