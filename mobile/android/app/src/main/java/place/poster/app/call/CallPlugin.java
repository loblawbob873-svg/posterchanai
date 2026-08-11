package place.poster.app.call;

import android.content.Intent;
import android.os.Build;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * The WebView's end of the in-call foreground service (see CallService for why a call needs one).
 *
 * Deliberately tiny. The call itself — the PeerConnection, the tracks, the Nostr signalling — is all
 * JS and stays there; this only starts and stops the thing that gives the process permission to keep
 * using the microphone while the app is not on screen, and forwards a Hang up press back.
 */
@CapacitorPlugin(name = "CallControls")
public class CallPlugin extends Plugin {

  @Override
  public void load() {
    CallService.setListener(action -> {
      JSObject data = new JSObject();
      data.put("action", action);
      notifyListeners("callAction", data);
    });
  }

  /**
   * A call has started (or changed). Safe to call repeatedly.
   *
   * The FIRST start goes through startForegroundService because there is no service yet; every later
   * update goes straight to the running instance. That split is not tidiness — Android 12+ throws
   * ForegroundServiceStartNotAllowedException on a background start, and an update can easily land
   * while the app is already off screen (the peer answers after you have pressed Home). The first
   * start is always made from a foreground app, because it is what placing or accepting a call does.
   */
  @PluginMethod
  public void start(PluginCall call) {
    boolean video = Boolean.TRUE.equals(call.getBoolean("video", false));
    String name = call.getString("name", "");
    String state = call.getString("state", "");
    CallService live = CallService.INSTANCE;
    if (live != null) {
      live.refresh(video, name, state);
      call.resolve();
      return;
    }
    Intent i = new Intent(getContext(), CallService.class)
        .setAction(CallService.ACTION_START)
        .putExtra(CallService.EXTRA_VIDEO, video)
        .putExtra(CallService.EXTRA_NAME, name)
        .putExtra(CallService.EXTRA_STATE, state);
    try {
      if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) getContext().startForegroundService(i);
      else getContext().startService(i);
      call.resolve();
    } catch (Throwable t) {
      // Report it rather than resolving: a call that silently has no background permission is
      // exactly the failure this plugin exists to prevent, and JS can at least say so.
      call.reject("could not start the call service: " + t.getMessage());
    }
  }

  /** The call ended. Never echoed back to JS — JS is what told us. */
  @PluginMethod
  public void stop(PluginCall call) {
    try {
      getContext().startService(new Intent(getContext(), CallService.class)
          .setAction(CallService.ACTION_STOP));
    } catch (Throwable ignored) {
      // A service that is already gone is the state we wanted.
    }
    call.resolve();
  }
}
