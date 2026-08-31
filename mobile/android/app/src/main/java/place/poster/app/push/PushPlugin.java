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

/**
 * Notifications for the packaged app.
 *
 * The WebView has NO Web Push. `pushManager.subscribe()` has nothing to subscribe to, so the native
 * build — the one users assume is the more capable of the two — could not receive a call or a message
 * while closed, at all, while the plain web PWA could. This closes that.
 *
 * PosterChan Direct uses one authenticated native socket to the instance. No Google account,
 * Firebase project, third-party distributor, compatibility process or resident WebView is involved.
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

    /**
     * Put "stay connected" back when the app is opened.
     *
     * FORCE-STOP IS ITS OWN CASE, and it is the one that was reported: "persistent notification not
     * working if you force close app and reopen, no notification". Force-stopping kills the service
     * AND puts the app into Android's "stopped" state, where it receives NO broadcasts at all until
     * the user launches it by hand — so BootReceiver never fires, and START_STICKY does not apply
     * either (the system takes a force-stop to mean the user wanted it stopped, and it is right to).
     *
     * The only moment left to restore it is the next time the app is opened, which is here. It runs
     * in the foreground, so the Android 12+ ban on background foreground-service starts does not
     * apply. `running` is a static that dies with the process, so after a force-stop it is false —
     * which is exactly the condition that should restart it, and which stops this doing anything on
     * an ordinary resume.
     *
     * Without it the switch in Settings reads "on" while nothing is running behind it, which is the
     * worst of both: no notifications, and a setting insisting there should be.
     */
    @Override
    public void load() {
        try {
            if (DirectPushService.configured(getContext()) && !DirectPushService.running) {
                DirectPushService.kick(getContext());
            }
            // Retain a user's independent media/background-connectivity setting across this update.
            if (StayAwakeService.wanted(getContext()) && !StayAwakeService.running) {
                Intent i = new Intent(getContext(), StayAwakeService.class)
                        .setAction(StayAwakeService.ACTION_START);
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) getContext().startForegroundService(i);
                else getContext().startService(i);
            }
        } catch (Throwable ignored) {
            // The switch is still on; opening Settings and toggling it is the way back, and the
            // stored preference means the next open tries again.
        }
    }


    /** Store server-issued direct credentials and start the native connection. */
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
        if (getPermissionState("notifications") != com.getcapacitor.PermissionState.GRANTED) {
            JSObject out = new JSObject();
            out.put("ok", false);
            out.put("error", "notification permission denied");
            call.resolve(out);
            return;
        }
        doRegister(call);
    }

    private void doRegister(PluginCall call) {
        Context ctx = getContext();
        JSObject out = new JSObject();
        try {
            String socketUrl = call.getString("socketUrl", "").trim();
            String token = call.getString("token", "").trim();
            String stable = DirectPushStore.deviceId(ctx);
            String requested = call.getString("deviceId", stable).trim();
            if (!stable.equals(requested)) throw new IllegalArgumentException("device id mismatch");
            if (!DirectPushService.safeSocketUrl(socketUrl)) {
                throw new IllegalArgumentException("notification socket must use wss");
            }
            if (token.isEmpty() || token.length() > 8192) {
                throw new IllegalArgumentException("invalid notification token");
            }
            DirectPushStore.save(ctx, socketUrl, token, stable);
            DirectPushService.kick(ctx);
            out.put("ok", true);
            out.put("direct", true);
            out.put("deviceId", stable);
            out.put("endpoint", "pcdirect:" + stable);
            out.put("connected", DirectPushService.connected);
        } catch (Throwable t) {
            out.put("ok", false);
            out.put("error", String.valueOf(t.getMessage()));
        }
        call.resolve(out);
    }

    /**
     * Stable id is available before registration so JavaScript can sign the server registration.
     * The endpoint is an opaque marker, never the socket URL or bearer token.
     */
    @PluginMethod
    public void getEndpoint(PluginCall call) {
        JSObject out = new JSObject();
        String device = DirectPushStore.deviceId(getContext());
        boolean configured = DirectPushService.configured(getContext());
        out.put("endpoint", configured ? "pcdirect:" + device : "");
        out.put("deviceId", device);
        out.put("direct", true);
        out.put("connected", DirectPushService.connected);
        out.put("error", DirectPushService.lastError);
        call.resolve(out);
    }

    @PluginMethod
    public void unregister(PluginCall call) {
        DirectPushStore.clear(getContext());
        try {
            getContext().startService(new Intent(getContext(), DirectPushService.class)
                    .setAction(DirectPushService.ACTION_STOP));
        } catch (Throwable ignored) { }
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
        String route = call.getString("route", "notifications");
        try {
            PushEventService.show(getContext(), title, body, type, tag, route);
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
