package place.poster.app.tor;

import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * Orbot (Tor for Android) integration.
 *
 * Deliberately thin. We do NOT proxy anything ourselves: the WebView has no per-app SOCKS setting, so
 * routing this app over Tor means Orbot's VPN mode, which is transparent to us — traffic (including
 * .onion name resolution, which Orbot answers itself) is already on Tor by the time our sockets see it.
 * There is therefore nothing to configure in-app and nothing that can silently half-apply.
 *
 * What the app still needs is the ability to TELL the user what's true, because the failure mode is
 * otherwise indistinguishable from a dead server: an .onion instance simply refuses to connect when
 * Orbot isn't running, with no hint as to why. So this exposes two facts and one action —
 *
 *   isInstalled() — is Orbot present? (needs the <queries> entry in AndroidManifest.xml; on API 30+
 *                   package visibility is opt-in and without it this reports false on a phone that
 *                   has Orbot sitting right there — the same trap Nip55Plugin documents.)
 *   start()       — ask Orbot to start. This is a REQUEST, not a guarantee: Orbot shows its own consent
 *                   UI, and the user must still add PosterChan to Orbot's app list (or use full-device
 *                   VPN mode). We report that we asked, never that Tor is up.
 *   openApp()     — foreground Orbot so the user can flip VPN mode / pick apps themselves.
 *
 * Whether traffic is ACTUALLY on Tor is not something an app can honestly self-report (checking would
 * mean an external request, which defeats the point), so the UI never claims it — it reports what is
 * installed and lets the connection itself be the proof.
 */
@CapacitorPlugin(name = "Orbot")
public class OrbotPlugin extends Plugin {

    private static final String ORBOT_PKG = "org.torproject.android";
    // Orbot's documented start request. It is advisory: Orbot may prompt, or ignore it if already running.
    private static final String ACTION_START = "org.torproject.android.intent.action.START";

    private boolean installed() {
        try {
            getContext().getPackageManager().getPackageInfo(ORBOT_PKG, 0);
            return true;
        } catch (PackageManager.NameNotFoundException e) {
            return false;
        } catch (Exception e) {
            return false;
        }
    }

    @PluginMethod
    public void isInstalled(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("installed", installed());
        ret.put("package", ORBOT_PKG);
        call.resolve(ret);
    }

    @PluginMethod
    public void start(PluginCall call) {
        JSObject ret = new JSObject();
        if (!installed()) {
            ret.put("requested", false);
            ret.put("installed", false);
            call.resolve(ret);
            return;
        }
        try {
            // setPackage is required: an implicit broadcast wouldn't be delivered on modern Android.
            Intent i = new Intent(ACTION_START);
            i.setPackage(ORBOT_PKG);
            getContext().sendBroadcast(i);
            ret.put("requested", true);
        } catch (Exception e) {
            ret.put("requested", false);
            ret.put("error", String.valueOf(e.getMessage()));
        }
        ret.put("installed", true);
        call.resolve(ret);
    }

    /** Foreground Orbot itself (its launcher activity), or its Play listing if it isn't installed. */
    @PluginMethod
    public void openApp(PluginCall call) {
        JSObject ret = new JSObject();
        try {
            Intent i = installed()
                ? getContext().getPackageManager().getLaunchIntentForPackage(ORBOT_PKG)
                : new Intent(Intent.ACTION_VIEW, Uri.parse("market://details?id=" + ORBOT_PKG));
            if (i == null) { ret.put("opened", false); call.resolve(ret); return; }
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(i);
            ret.put("opened", true);
        } catch (Exception e) {
            ret.put("opened", false);
            ret.put("error", String.valueOf(e.getMessage()));
        }
        call.resolve(ret);
    }
}
