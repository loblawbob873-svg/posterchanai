package place.poster.app.screenshare;

import android.Manifest;
import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.media.projection.MediaProjectionManager;
import android.os.Build;

import androidx.activity.result.ActivityResult;
import androidx.core.content.ContextCompat;

import com.getcapacitor.JSObject;
import com.getcapacitor.PermissionState;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.ActivityCallback;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

/**
 * Screen sharing for the live-stream feature (see ScreenShareService for why this can't be done in the WebView).
 *
 * Consent and start are DELIBERATELY two calls. Going live from the screen while already live from the camera
 * means handing the same MediaMTX path over from the WebRTC publisher to the RTMP one, and MediaMTX only
 * allows one publisher per path — so the camera has to be torn down BEFORE the screen starts. If start() also
 * did the consent dialog, a user who cancelled it would be left with no camera and no screen: a dead stream.
 * So JS asks for consent first, and only tears the camera down once the user has actually said yes.
 */
@CapacitorPlugin(
    name = "ScreenShare",
    permissions = {
        @Permission(alias = "microphone", strings = { Manifest.permission.RECORD_AUDIO }),
        @Permission(alias = "notifications", strings = { Manifest.permission.POST_NOTIFICATIONS })
    }
)
public class ScreenSharePlugin extends Plugin {

  // The user's grant, held between requestConsent() and start(). Android 14+ allows each consent to be turned
  // into a MediaProjection exactly ONCE, so this is cleared the moment it's handed to the service.
  private Intent consentData;
  private int consentCode;

  @Override
  public void load() {
    ScreenShareService.setListener((event, message) -> {
      JSObject data = new JSObject();
      data.put("event", event);
      data.put("message", message == null ? "" : message);
      notifyListeners("screenShareStatus", data);
    });
  }

  /** Show the system's screen-capture dialog. Resolves {granted} — it does NOT start streaming. */
  @PluginMethod
  public void requestConsent(PluginCall call) {
    if (getPermissionState("microphone") != PermissionState.GRANTED) {
      requestPermissionForAlias("microphone", call, "micPermission");
      return;
    }
    if (Build.VERSION.SDK_INT >= 33 && getPermissionState("notifications") != PermissionState.GRANTED) {
      requestPermissionForAlias("notifications", call, "notifPermission");
      return;
    }
    askForScreen(call);
  }

  @PermissionCallback
  private void micPermission(PluginCall call) {
    // The mic is the voiceover over the shared screen. Without it we'd have to drop the microphone foreground
    // -service type, so treat a refusal as a refusal of the whole feature rather than silently going mute.
    if (getPermissionState("microphone") != PermissionState.GRANTED) {
      call.reject("microphone permission is needed to share your screen with sound");
      return;
    }
    requestConsent(call);
  }

  @PermissionCallback
  private void notifPermission(PluginCall call) {
    askForScreen(call);   // a denied notification permission doesn't stop the service — carry on
  }

  private void askForScreen(PluginCall call) {
    MediaProjectionManager mpm = (MediaProjectionManager)
        getContext().getSystemService(Context.MEDIA_PROJECTION_SERVICE);
    if (mpm == null) { call.reject("this device can't capture the screen"); return; }
    // A FRESH intent every time: Android 14+ throws if a cached one is reused.
    startActivityForResult(call, mpm.createScreenCaptureIntent(), "consentResult");
  }

  @ActivityCallback
  private void consentResult(PluginCall call, ActivityResult result) {
    if (call == null) return;
    JSObject ret = new JSObject();
    if (result.getResultCode() != Activity.RESULT_OK || result.getData() == null) {
      ret.put("granted", false);
      call.resolve(ret);      // a cancel is a normal outcome, not an error
      return;
    }
    consentCode = result.getResultCode();
    consentData = result.getData();
    ret.put("granted", true);
    call.resolve(ret);
  }

  /** Start pushing the screen to `url` (rtmp://host:port/<token>?key=<api_key>). Needs a prior consent. */
  @PluginMethod
  public void start(PluginCall call) {
    String url = call.getString("url");
    if (url == null || url.isEmpty()) { call.reject("url is required"); return; }
    if (consentData == null) { call.reject("screen capture was not granted"); return; }

    Intent svc = new Intent(getContext(), ScreenShareService.class);
    svc.setAction(ScreenShareService.ACTION_START);
    svc.putExtra(ScreenShareService.EXTRA_RESULT_CODE, consentCode);
    svc.putExtra(ScreenShareService.EXTRA_RESULT_DATA, consentData);
    svc.putExtra(ScreenShareService.EXTRA_URL, url);
    consentData = null;       // one MediaProjection per consent — never reuse it
    ContextCompat.startForegroundService(getContext(), svc);
    call.resolve();           // 'connected'/'error' follow asynchronously on the screenShareStatus listener
  }

  @PluginMethod
  public void stop(PluginCall call) {
    Intent svc = new Intent(getContext(), ScreenShareService.class);
    svc.setAction(ScreenShareService.ACTION_STOP);
    try { getContext().startService(svc); } catch (Exception ignored) {}
    call.resolve();
  }

  @PluginMethod
  public void isStreaming(PluginCall call) {
    ScreenShareService svc = ScreenShareService.INSTANCE;
    JSObject ret = new JSObject();
    ret.put("value", svc != null && svc.isStreaming());
    call.resolve(ret);
  }
}
