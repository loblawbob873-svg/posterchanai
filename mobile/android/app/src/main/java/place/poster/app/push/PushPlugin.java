package place.poster.app.push;

import android.Manifest;
import android.app.ActivityManager;
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
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import org.unifiedpush.android.connector.UnifiedPush;

/**
 * Notifications for the packaged app.
 *
 * The WebView has NO Web Push. `pushManager.subscribe()` has nothing to subscribe to, so the native
 * build — the one users assume is the more capable of the two — could not receive a call or a message
 * while closed, at all, while the plain web PWA could. This closes that.
 *
 * UnifiedPush rather than Firebase: an endpoint is an ordinary HTTPS URL issued by a distributor the
 * USER installed and chose (ntfy and friends), so delivery is a POST from our own server. No Google
 * account, no Firebase project, no proprietary SDK in a self-hosted app — and the distributor holds
 * ONE OS-level socket shared by every app on the phone, which is why this costs far less battery than
 * us keeping a relay connection alive in the background would.
 *
 * The endpoint arrives asynchronously, at the distributor's leisure — see PushEventService, which
 * is what actually hands it to the web layer to register with the server.
 *
 * The battery half is here for a reason that is not obvious: Samsung's "Deep sleeping apps" (and the
 * generic background restriction) FORCE-STOP the app. A force-stopped process receives no pushes and
 * cannot have its AutofillService bound, so one toggle the user flipped months ago silently disables
 * both incoming calls and password autofill, with no error anywhere. It is the single most common bug
 * report password managers get. Reporting it is the difference between "the app is broken" and one
 * tap to fix it.
 */
@CapacitorPlugin(
    name = "PosterChanPush",
    permissions = {
        @Permission(alias = "notifications", strings = { Manifest.permission.POST_NOTIFICATIONS })
    }
)
public class PushPlugin extends Plugin {

    private static final String INSTANCE = "default";

    /** Ask the user's distributor for an endpoint. Result arrives via PushEventService. */
    @PluginMethod
    public void register(PluginCall call) {
        // Android 13+ DROPS every notification unless this was granted at runtime — declaring it in
        // the manifest is not enough. Without this, registration succeeds and the phone stays silent,
        // which is indistinguishable from a delivery failure.
        if (getPermissionState("notifications") != com.getcapacitor.PermissionState.GRANTED) {
            requestPermissionForAlias("notifications", call, "afterNotifPermission");
            return;
        }
        doRegister(call);
    }

    @PermissionCallback
    private void afterNotifPermission(PluginCall call) {
        doRegister(call);   // proceed either way: a refused permission still registers the endpoint,
                            // and the in-app check reports why nothing is appearing.
    }

    private void doRegister(PluginCall call) {
        Context ctx = getContext();
        JSObject out = new JSObject();
        try {
            // No distributor installed = nothing can deliver to this phone. Say so plainly rather
            // than register into a void and look like a delivery failure later.
            java.util.List<String> dists = UnifiedPush.getDistributors(ctx);
            if (dists.isEmpty()) {
                out.put("ok", false);
                out.put("needsDistributor", true);
                call.resolve(out);
                return;
            }
            // getDistributor() returns the ACKed one, which stays empty until the first endpoint
            // arrives — so re-registering would otherwise overwrite the user's choice with dists[0].
            String saved = UnifiedPush.getSavedDistributor(ctx);   // nullable in 3.x
            if (saved == null || saved.isEmpty()) {
                UnifiedPush.saveDistributor(ctx, dists.get(0));
            }
            // All four arguments spelled out: the Kotlin defaults are not guaranteed visible to Java.
            UnifiedPush.register(ctx, INSTANCE, null, null);
            out.put("ok", true);
        } catch (Throwable t) {
            out.put("ok", false);
            out.put("error", String.valueOf(t.getMessage()));
        }
        call.resolve(out);
    }

    /**
     * The endpoint the distributor issued, or "" if it hasn't arrived yet.
     *
     * register() cannot return it: the distributor answers whenever it likes, into a broadcast
     * receiver, possibly after this call has long returned. So the receiver stashes it and the web
     * layer — the only side holding the Nostr key needed to sign the registration — polls for it.
     */
    @PluginMethod
    public void getEndpoint(PluginCall call) {
        JSObject out = new JSObject();
        out.put("endpoint", getContext()
                .getSharedPreferences(PushEventService.PREFS, Context.MODE_PRIVATE)
                .getString(PushEventService.KEY_ENDPOINT, ""));
        call.resolve(out);
    }

    @PluginMethod
    public void unregister(PluginCall call) {
        try {
            UnifiedPush.unregister(getContext(), INSTANCE);
        } catch (Throwable ignored) {
        }
        // unregister() does not clear OUR copy of the endpoint, and pushState() reads that — without
        // this, turning notifications off leaves the toggle reading "on".
        getContext().getSharedPreferences(PushEventService.PREFS, Context.MODE_PRIVATE)
                    .edit().remove(PushEventService.KEY_ENDPOINT).apply();
        call.resolve();
    }

    /**
     * Can this app actually be woken? Both answers matter and neither is visible to the web layer.
     *
     * backgroundRestricted — the user (or the OEM's battery screen) has barred background work. On
     *                        Samsung this is what "Deep sleeping apps" sets, and it is fatal.
     * ignoringOptimizations — false means Doze may defer our wakeups; survivable, but worth telling
     *                        someone who reports late notifications.
     */
    @PluginMethod
    public void batteryStatus(PluginCall call) {
        Context ctx = getContext();
        JSObject out = new JSObject();
        boolean restricted = false, ignoring = true;
        try {
            ActivityManager am = (ActivityManager) ctx.getSystemService(Context.ACTIVITY_SERVICE);
            if (am != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
                restricted = am.isBackgroundRestricted();
            }
            PowerManager pm = (PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
            if (pm != null && Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
                ignoring = pm.isIgnoringBatteryOptimizations(ctx.getPackageName());
            }
        } catch (Throwable ignored) {
        }
        out.put("backgroundRestricted", restricted);
        out.put("ignoringBatteryOptimizations", ignoring);
        out.put("healthy", !restricted && ignoring);
        call.resolve(out);
    }

    /**
     * Open the OS screen where the user can undo the above. Deliberately the app's own settings page
     * rather than REQUEST_IGNORE_BATTERY_OPTIMIZATIONS: that permission gets apps pulled from Play,
     * and it cannot clear an OEM "deep sleep" flag anyway — only the user can, on this screen.
     */
    @PluginMethod
    public void openBatterySettings(PluginCall call) {
        try {
            Intent i = new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                                  Uri.parse("package:" + getContext().getPackageName()));
            i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            getContext().startActivity(i);
            call.resolve();
        } catch (Throwable t) {
            call.reject("could not open settings: " + t.getMessage());
        }
    }

    /**
     * Raise a notification FROM THE WEB LAYER.
     *
     * Android's WebView does not implement the Notifications API — `new Notification(...)` in there is
     * not an error, it is silence. So the client's one notification helper (osNotify) drew nothing in
     * the APK: a DM that had already arrived on the open relay socket produced a toast if you were
     * looking and nothing at all if you were not. Same shape as the media-controls gap, same fix.
     *
     * Goes through PushEventService.show — the SAME builder a real push uses — so a notification looks
     * and behaves identically whether it came from the server or from a socket this app already had.
     */
    @PluginMethod
    public void notify(PluginCall call) {
        String title = call.getString("title", "PosterChan");
        String body = call.getString("body", "");
        String type = call.getString("type", "");
        String tag = call.getString("tag", null);
        try {
            PushEventService.show(getContext(), title, body, type, tag);
            call.resolve();
        } catch (Throwable t) {
            call.reject("could not show a notification: " + t.getMessage());
        }
    }

    /**
     * Turn the persistent "stay connected" foreground service on or off (see StayAwakeService).
     *
     * The START must come from the foreground — Android 12+ refuses a background foreground-service
     * start — which is fine, because the only thing that calls this is a switch in Settings.
     */
    @PluginMethod
    public void setStayConnected(PluginCall call) {
        boolean on = Boolean.TRUE.equals(call.getBoolean("on", false));
        Intent i = new Intent(getContext(), StayAwakeService.class)
                .setAction(on ? StayAwakeService.ACTION_START : StayAwakeService.ACTION_STOP);
        try {
            if (on && Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) getContext().startForegroundService(i);
            else getContext().startService(i);
            JSObject out = new JSObject();
            out.put("on", on);
            call.resolve(out);
        } catch (Throwable t) {
            // Say so rather than resolving: silently not staying connected is the failure the whole
            // switch exists to prevent.
            StayAwakeService.setWanted(getContext(), false);
            call.reject("could not change it: " + t.getMessage());
        }
    }

    @PluginMethod
    public void stayConnected(PluginCall call) {
        JSObject out = new JSObject();
        // The REMEMBERED preference, not `running`: Android may have killed the service, and a switch
        // that flips itself off because of that would tell the user they turned something off.
        out.put("on", StayAwakeService.wanted(getContext()));
        out.put("running", StayAwakeService.running);
        call.resolve(out);
    }
}
