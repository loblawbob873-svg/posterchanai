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

  /** Who installed this APK — "" when Android will not say (sideload, old API, hardened ROM).
   *  The updater branches on it: an install that came from a STORE should update through that
   *  store (it verifies the signature and tracks versions), not by sideloading over itself. */
  @PluginMethod
  public void installer(PluginCall call) {
    String who = "";
    try {
      android.content.pm.PackageManager pm = getContext().getPackageManager();
      String pkg = getContext().getPackageName();
      if (android.os.Build.VERSION.SDK_INT >= 30) {
        who = pm.getInstallSourceInfo(pkg).getInstallingPackageName();
      } else {
        who = pm.getInstallerPackageName(pkg);
      }
    } catch (Throwable ignored) { }
    com.getcapacitor.JSObject out = new com.getcapacitor.JSObject();
    out.put("installer", who == null ? "" : who);
    call.resolve(out);
  }

  /** Launch another installed app by package id — the updater uses it to open the STORE that
   *  installed this APK (Zapstore et al.) instead of that store's website in a browser tab.
   *  ok:false when the package has no launch intent here, so the caller can fall back. */
  @PluginMethod
  public void launch(PluginCall call) {
    String pkg = call.getString("pkg", "");
    boolean ok = false;
    try {
      Intent i = getContext().getPackageManager().getLaunchIntentForPackage(pkg);
      if (i != null) { i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK); getContext().startActivity(i); ok = true; }
    } catch (Throwable ignored) { }
    com.getcapacitor.JSObject out = new com.getcapacitor.JSObject();
    out.put("ok", ok);
    call.resolve(out);
  }

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
