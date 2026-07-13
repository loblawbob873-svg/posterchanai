package place.poster.app.share;

import android.content.Intent;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import place.poster.app.MainActivity;

/**
 * Which share target did the user pick?
 *
 * The app exposes two entries in the OS share sheet — "PosterChan" (compose a post) and "PosterChan AI" (drop
 * it into the AI chat) — as an activity-alias over the same MainActivity. The share payload itself arrives via
 * the send-intent plugin, which says nothing about WHICH door it came through; that's only in the launch
 * intent's component. So read it here.
 *
 * MainActivity.onNewIntent() calls setIntent(), so getIntent() is the CURRENT share even when the app was
 * already running — the same reason the send-intent plugin works on a warm share.
 */
@CapacitorPlugin(name = "ShareTarget")
public class ShareTargetPlugin extends Plugin {

  private static final String AI_ALIAS = "place.poster.app.ShareToAi";

  @PluginMethod
  public void getTarget(PluginCall call) {
    String cls = "";
    try {
      Intent intent = getActivity().getIntent();
      if (intent != null && intent.getComponent() != null) {
        cls = intent.getComponent().getClassName();
      }
    } catch (Exception ignored) {
    }
    JSObject ret = new JSObject();
    ret.put("target", AI_ALIAS.equals(cls) ? "ai" : "post");
    // Per-share nonce: JS dedups on this so re-sharing the SAME file is a new share, not a swallowed duplicate.
    ret.put("nonce", MainActivity.shareNonce);
    call.resolve(ret);
  }
}
