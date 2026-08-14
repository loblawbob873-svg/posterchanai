package place.poster.app.signer;

import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.PowerManager;
import android.provider.Settings;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * How the client hands its key to the native signer, and asks what the phone holds.
 *
 * ONE DIRECTION ONLY: the secret goes IN and never comes back out. There is no `getKey`, and adding
 * one would undo the entire point — the key is here precisely so that a script in the WebView cannot
 * read it. `status()` answers with the PUBLIC key, which is public.
 *
 * It also carries the paired-app list ACROSS the same boundary in one direction: the page knows which
 * apps are paired (it does the QR scanning and the consent), the service knows how to stay awake, and
 * `sync()` is the only thing joining them. The service treats that list as authoritative about
 * MEMBERSHIP and keeps its own learned fields — see `Nip46Core.merge`, which exists because a
 * straight replace would discard what only the awake half can know.
 */
@CapacitorPlugin(name = "Signer")
public class SignerPlugin extends Plugin {

    @PluginMethod
    public void status(PluginCall call) {
        JSObject o = new JSObject();
        o.put("have", SignerKey.have(getContext()));
        o.put("pubkey", SignerKey.pubkey(getContext()));
        // What the SERVICE measured, never what the page assumed. A panel that reports the page's
        // intention is how "the signer is on" sat above a signer that had answered nothing for hours.
        o.put("serviceRunning", SignerRelayService.running);
        o.put("serviceWanted", SignerRelayService.wanted(getContext()));
        o.put("connected", SignerRelayService.connected);
        o.put("answered", SignerRelayService.requestsAnswered);
        o.put("lastRequestAt", SignerRelayService.lastRequestAt);
        o.put("lastError", SignerRelayService.lastError);
        o.put("batteryExempt", batteryExempt());
        call.resolve(o);
    }

    /** `sec` is 32 bytes of hex. Returns the x-only pubkey so the UI can show whose key landed. */
    @PluginMethod
    public void enable(PluginCall call) {
        String sec = call.getString("sec");
        if (sec == null || sec.length() != 64) { call.reject("need a 32-byte hex secret"); return; }
        try {
            String pub = SignerKey.store(getContext(), Nostr.unhex(sec));
            JSObject o = new JSObject();
            o.put("pubkey", pub);
            call.resolve(o);
        } catch (Throwable t) {
            // Say which step failed, without ever echoing the input back.
            call.reject("could not store the key on this device");
        }
    }

    @PluginMethod
    public void disable(PluginCall call) {
        SignerKey.clear(getContext());
        // A key that is gone cannot sign, so the service has nothing left to do. Leaving it up would
        // hold sockets open for a signer that must refuse every request.
        SignerRelayService.kick(getContext(), SignerRelayService.ACTION_STOP);
        call.resolve();
    }

    /**
     * Publish the paired apps and (re)start the service so it answers them without the page.
     *
     * Called whenever a pairing is made or revoked, and once at sign-in. `sessions` is the same JSON
     * the web signer persists, minus anything secret.
     */
    @PluginMethod
    public void sync(PluginCall call) {
        String json = call.getString("sessions");
        Context ctx = getContext();
        SignerRelayService.publishSessions(ctx, json == null ? "[]" : json);
        boolean on = call.getBoolean("enabled", Boolean.TRUE);
        if (Boolean.FALSE.equals(on)) {
            SignerRelayService.kick(ctx, SignerRelayService.ACTION_STOP);
        } else {
            SignerRelayService.kick(ctx, SignerRelayService.running
                    ? SignerRelayService.ACTION_RELOAD : SignerRelayService.ACTION_START);
        }
        JSObject o = new JSObject();
        o.put("running", SignerRelayService.running);
        call.resolve(o);
    }

    @PluginMethod
    public void stopService(PluginCall call) {
        SignerRelayService.kick(getContext(), SignerRelayService.ACTION_STOP);
        call.resolve();
    }

    /**
     * Whether Android has this app exempt from Doze's app-standby buckets.
     *
     * THE HONEST LIMIT OF A FOREGROUND SERVICE. It keeps the process resident and its socket open,
     * but on many OEM builds — and under Doze on stock — an unexempted app's network is still
     * deferred while the screen is off, so a signing request can wait for the next maintenance
     * window. Amber asks for this exemption for exactly the same reason. Reported here rather than
     * requested silently: it is the user's call, and a signer that quietly asks for battery
     * exemptions is a signer that deserves suspicion.
     */
    private boolean batteryExempt() {
        try {
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) return true;
            PowerManager pm = (PowerManager) getContext().getSystemService(Context.POWER_SERVICE);
            return pm != null && pm.isIgnoringBatteryOptimizations(getContext().getPackageName());
        } catch (Throwable t) { return false; }
    }

    /**
     * Send the user to the system screen that grants it. NEVER the direct-request Intent
     * (`ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`): Google Play bans it outside a short list of
     * app categories, and a signer is not on that list — shipping it risks the listing over a dialog
     * the settings screen offers anyway.
     */
    @PluginMethod
    public void openBatterySettings(PluginCall call) {
        try {
            Intent i = new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS);
            i.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(i);
            call.resolve();
        } catch (Throwable t) {
            try {
                Intent i = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                        Uri.parse("package:" + getContext().getPackageName()));
                i.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                getContext().startActivity(i);
                call.resolve();
            } catch (Throwable t2) {
                call.reject("could not open the battery settings screen");
            }
        }
    }
}
