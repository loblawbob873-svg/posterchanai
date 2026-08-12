package place.poster.app.music;

import android.Manifest;
import android.content.Intent;
import android.os.Build;

import androidx.core.content.ContextCompat;

import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

/**
 * The WebView's end of the media controls (see MusicService for why a WebView needs a native half).
 *
 * Two directions, and they are not symmetric:
 *   update()  — the player's state, pushed on every track change, play/pause, and once a second while
 *               playing. Cheap by design: it is one Intent into an already-running service.
 *   listeners — `musicTransport` events, one per button press anywhere OUTSIDE the app (lock screen,
 *               shade, headset, car, widget). JS performs them on the audio element and the resulting
 *               state comes back through update(), so the notification can never disagree with what
 *               is actually playing.
 */
@CapacitorPlugin(
    name = "MusicControls",
    permissions = { @Permission(alias = "notifications", strings = { Manifest.permission.POST_NOTIFICATIONS }) }
)
public class MusicPlugin extends Plugin {

  /** Set on the launch Intent by the widget — and by MusicService when a press went unanswered and
   *  it had to wake the app to get it performed; read once by JS (see consumeLaunchAction). */
  public static final String EXTRA_LAUNCH_ACTION = "pc_music_action";
  /** When that press was made. An intent delivered to an app that is ALREADY on screen fires
   *  onNewIntent and then sits there — nothing re-reads it until the next resume — so without this a
   *  press could be performed minutes later, starting music over somebody who had since paused it. */
  public static final String EXTRA_LAUNCH_AT = "pc_music_action_at";
  /** How old a parked press may be before it is dropped rather than performed. */
  private static final long LAUNCH_MAX_AGE_MS = 60_000;

  private boolean askedForNotifications = false;

  @Override
  public void load() {
    MusicService.setListener((action, value) -> {
      JSObject data = new JSObject();
      data.put("action", action);
      data.put("value", value);
      notifyListeners("musicTransport", data);
    });
  }

  /**
   * Publish what the player is doing.
   *
   * Called ~once a second, so it must stay a no-op-shaped call: no work here beyond an Intent, and
   * the permission prompt below fires at most once per process. Asking for POST_NOTIFICATIONS on the
   * FIRST update rather than at app start is deliberate — the request then arrives with the reason
   * on screen (the user just pressed play), which is the difference between a granted and a
   * reflexively dismissed prompt. A refusal is not fatal: the media session still exists, so the
   * lock screen, headset buttons and the widget all keep working; only the shade entry is missing.
   */
  @PluginMethod
  public void update(PluginCall call) {
    if (Build.VERSION.SDK_INT >= 33 && !askedForNotifications
        && getPermissionState("notifications") != PermissionState.GRANTED) {
      askedForNotifications = true;
      requestPermissionForAlias("notifications", call, "notifPermission");
      return;
    }
    push(call);
  }

  @PermissionCallback
  private void notifPermission(PluginCall call) { push(call); }

  private void push(PluginCall call) {
    String title = call.getString("title", "");
    String artist = call.getString("artist", "PosterChan");
    boolean playing = Boolean.TRUE.equals(call.getBoolean("playing", false));
    double position = call.getDouble("position", 0.0);
    double duration = call.getDouble("duration", 0.0);

    MusicService svc = MusicService.INSTANCE;
    if (svc != null) {
      // Already running, and in THIS process — a direct call. Going back through
      // startForegroundService once a second would be refused the moment the screen went off (see
      // MusicService.apply), which is exactly when the lock screen is all the user has.
      svc.apply(title, artist, playing, position, duration);
      call.resolve();
      return;
    }

    Intent i = new Intent(getContext(), MusicService.class)
        .setAction(MusicService.ACTION_UPDATE)
        .putExtra(MusicService.EXTRA_TITLE, title)
        .putExtra(MusicService.EXTRA_ARTIST, artist)
        .putExtra(MusicService.EXTRA_PLAYING, playing)
        .putExtra(MusicService.EXTRA_POSITION, position)
        .putExtra(MusicService.EXTRA_DURATION, duration);
    try {
      // The one start that needs the Intent. It happens with the app on screen — playback begins
      // from a tap in the UI — which is the one state Android 12+ allows an FGS to be started from.
      ContextCompat.startForegroundService(getContext(), i);
    } catch (Exception e) {
      // A background start the platform refused. The music is unaffected — it plays in the WebView —
      // so this is a lost notification, not a lost track: report it and let JS carry on.
      call.reject("could not show the media controls: " + e.getMessage());
      return;
    }
    call.resolve();
  }

  /** Playback ended (or the player was closed): drop the notification and idle the widget. */
  @PluginMethod
  public void stop(PluginCall call) {
    MusicService svc = MusicService.INSTANCE;
    if (svc != null) {
      Intent i = new Intent(getContext(), MusicService.class).setAction(MusicService.ACTION_STOP);
      try { getContext().startService(i); } catch (Exception ignored) { svc.shutdown(); }
    } else {
      // Never started (or already gone) — but a widget may still be showing the last track.
      MusicWidget.render(getContext(), null, null, false, false);
    }
    call.resolve();
  }

  /**
   * The client's options. Persisted natively (see MusicService.setAutoplayBluetooth) because the
   * service outlives the page that set them, and a reloaded WebView pushes them again on the way up.
   */
  @PluginMethod
  public void setOptions(PluginCall call) {
    if (call.hasOption("autoplayBluetooth")) {
      MusicService.setAutoplayBluetooth(getContext(),
          Boolean.TRUE.equals(call.getBoolean("autoplayBluetooth", false)));
    }
    call.resolve();
  }

  /**
   * WHAT THE PHONE ACTUALLY MEASURED. This feature fails by reporting success from every side — the
   * notification is up, the emit returned, and no sound comes out — and there is no device here to
   * watch it happen on. So the counters the service keeps are readable from the app: whether the
   * service is running at all, how long since the client last answered it, how many presses went
   * unanswered, how many times the app had to be woken, and what the last Bluetooth connection did.
   */
  @PluginMethod
  public void status(PluginCall call) {
    JSObject r = new JSObject();
    MusicService svc = MusicService.INSTANCE;
    r.put("running", svc != null);
    r.put("autoplayBluetooth", MusicService.autoplayBluetooth(getContext()));
    /* The counters are STATIC, and read whether or not a service is alive right now. The case this
     * panel exists to explain is the cold one — a press made in the car with the app closed — and
     * that path deliberately ends in stopSelf(), so reading them off the instance answered "not
     * running, nothing has played this session" about the very press being investigated. */
    r.put("btConnects", MusicService.btConnects);
    r.put("btAutoplays", MusicService.btAutoplays);
    r.put("unanswered", MusicService.unanswered);
    r.put("revived", MusicService.revived);
    r.put("note", MusicService.note);
    if (svc != null) {
      r.put("playing", svc.isPlaying());
      r.put("webSilenceMs", svc.webSilenceMs());
      r.put("webGone", svc.webGone());
    }
    call.resolve(r);
  }

  /**
   * "I am still here." The client's answer to a transport event — see MusicService.ack for why this
   * is not just another update(): a page that has reloaded holds no track, so it has no state to
   * push, and a client that cannot push is not a client that is gone.
   */
  @PluginMethod
  public void ack(PluginCall call) {
    MusicService svc = MusicService.INSTANCE;
    if (svc != null) svc.ack();
    call.resolve();
  }

  /**
   * What the widget was tapped with, if the app was launched by it. CONSUMED — returned once and
   * cleared, or a later resume would re-fire the same press and restart the music under someone who
   * had paused it.
   */
  @PluginMethod
  public void consumeLaunchAction(PluginCall call) {
    JSObject ret = new JSObject();
    String action = null;
    try {
      Intent i = getActivity() == null ? null : getActivity().getIntent();
      if (i != null) {
        action = i.getStringExtra(EXTRA_LAUNCH_ACTION);
        long at = i.getLongExtra(EXTRA_LAUNCH_AT, 0);
        if (action != null) { i.removeExtra(EXTRA_LAUNCH_ACTION); i.removeExtra(EXTRA_LAUNCH_AT); }
        // Stale = never performed. A press parked in an intent is a press the user has moved on from.
        if (action != null && at > 0 && System.currentTimeMillis() - at > LAUNCH_MAX_AGE_MS) action = null;
      }
    } catch (Exception ignored) {}
    ret.put("action", action == null ? "" : action);
    call.resolve(ret);
  }
}
