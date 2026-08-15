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
        /* An empty list is a stop, not a start. The service would work this out for itself and stop
         * immediately (see reload), but starting it first means going foreground and posting a
         * notification just to tear both down — a visible flash of "PosterChan signer" on every
         * sign-in for someone who has never paired anything. */
        String trimmed = json == null ? "" : json.trim();
        if (trimmed.isEmpty() || "[]".equals(trimmed)) on = false;
        if (Boolean.FALSE.equals(on)) {
            SignerRelayService.kick(ctx, SignerRelayService.ACTION_STOP);
        } else {
            SignerRelayService.kick(ctx, SignerRelayService.running
                    ? SignerRelayService.ACTION_RELOAD : SignerRelayService.ACTION_START);
        }
        JSObject o = new JSObject();
        o.put("running", SignerRelayService.running);
        /* HOW MANY SOCKETS IT ACTUALLY HOLDS, because `running` alone is not a hand-over receipt.
         *
         * `kick()` is startService — asynchronous — so this reads a flag the service may not have
         * touched yet, and even once it is true the sockets are opened later still, on the work
         * thread. The caller uses this to decide whether to CLOSE ITS OWN sockets, and closing them
         * against a service that is up but not yet subscribed leaves nobody answering at all.
         * Reported as "even with the app open, my drafts from desktop is not getting sent". */
        o.put("connected", SignerRelayService.connected);
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
     * Ask for the battery exemption — the ONE tap, not a list of every app on the phone.
     *
     * THIS REVERSES AN EARLIER DECISION, so the reason is recorded rather than the conclusion. The
     * direct-request Intent (`ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`) was avoided here because
     * Google Play bans it outside a short list of app categories and a signer is not on that list.
     * That constraint does not apply to THIS app: it ships as an APK through Zapstore and GitHub
     * Releases (.github/workflows/android.yml runs `assembleRelease`; there is no .aab and no Play
     * Console anywhere in the repo). We were paying a Play tax without being on Play.
     *
     * It matters because of what the alternative actually costs. The settings-list screen drops
     * somebody into an alphabetical list of every installed app to find PosterChan themselves, for a
     * setting that is the ONLY thing standing between a working background signer and one that does
     * nothing while the screen is off — an "Optimized" app has its NETWORK deferred by Doze, so the
     * request sits on the relay until a maintenance window and the other device shows "waiting for
     * your signer…". Reported exactly that way, and the phone had quietly set PosterChan to
     * Optimized on its own; OEMs re-apply it after updates, so this is not a one-time setup step.
     *
     * The list is kept as the FALLBACK, because the direct dialog is refused outright on some ROMs
     * (and is a no-op if the exemption is already held), and landing nowhere would be worse than
     * landing on a list.
     */
    @PluginMethod
    public void openBatterySettings(PluginCall call) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !batteryExempt()) {
                @SuppressWarnings("BatteryLife")
                Intent d = new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                        Uri.parse("package:" + getContext().getPackageName()));
                d.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
                getContext().startActivity(d);
                call.resolve();
                return;
            }
        } catch (Throwable ignored) {
            // Refused by the ROM, or no activity to handle it — fall through to the list below.
        }
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
