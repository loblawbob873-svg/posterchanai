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

  /** Set on the launch Intent by the widget; read once by JS (see consumeLaunchAction). */
  public static final String EXTRA_LAUNCH_ACTION = "pc_music_action";

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
        if (action != null) i.removeExtra(EXTRA_LAUNCH_ACTION);
      }
    } catch (Exception ignored) {}
    ret.put("action", action == null ? "" : action);
    call.resolve(ret);
  }
}
