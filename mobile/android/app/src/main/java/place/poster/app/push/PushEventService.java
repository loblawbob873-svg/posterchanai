package place.poster.app.push;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

import org.json.JSONObject;

import place.poster.app.MainActivity;

/**
 * The one renderer for every native notification.
 *
 * The server sends the SAME JSON payload it sends the service worker — {title, body, type} — so there
 * is one notification contract for every transport rather than a second one that can drift out of
 * step with the web client's.
 *
 * `type` decides the treatment: a call is HIGH importance with a full-screen intent so it behaves like
 * an incoming call rather than an email, everything else is an ordinary heads-up. Two channels, so the
 * user can silence chatter without silencing calls — Android only lets them do that per channel.
 */
public final class PushEventService {

    public static final String PREFS = "pcai_push";
    private static final String CH_CALLS = "pcai_calls";
    private static final String CH_MSGS = "pcai_messages";

    private PushEventService() { }

    /**
     * Render the compact JSON contract delivered by PosterChan Direct. Keeping parsing here means a
     * foreground relay notification and a notification raised by the visible client still use the
     * exact same channels, de-duplication tags and deep links.
     */
    public static boolean deliver(Context ctx, String payload) {
        String title = "PosterChan", body = "New activity", type = "", route = "notifications";
        String eventTag = null;
        try {
            JSONObject j = new JSONObject(payload == null ? "{}" : payload);
            // The direct server currently sends the payload itself. Accept the explicit envelope too
            // so a protocol version can add control frames without changing notification rendering.
            JSONObject nested = j.optJSONObject("payload");
            if (nested != null && "notification".equals(j.optString("type"))) j = nested;
            title = j.optString("title", title);
            body = j.optString("body", body);
            type = j.optString("type", "");
            String eid = j.optString("eid", "").trim();
            eventTag = !eid.isEmpty() ? "nostr-" + eid : null;
            route = !eid.isEmpty() ? "post:" + eid : j.optString("view", route).trim();
        } catch (Throwable ignored) {
            // A payload we cannot parse is still a signal that SOMETHING happened; showing the
            // default beats swallowing it, which would look exactly like a delivery failure.
        }
        // PosterChan Direct and the visible WebView relay can observe the SAME Nostr event. Both use
        // the event-id tag, so Android replaces the duplicate card while distinct events coexist.
        try {
            show(ctx, title, body, type, eventTag, route);
            return true;
        } catch (Throwable ignored) {
            return false;
        }
    }

    /**
     * Draw one notification. THE ONLY BUILDER, and that is the point.
     *
     * A push and a notification raised by the WEB LAYER (see PushPlugin.notify) have to look and
     * behave identically — same channels, same call treatment, same tap target — or a person gets two
     * different notifications for the same event depending on whether the app happened to be running.
     * The web layer needs its own route because Android's WebView does not implement the Notifications
     * API at all: `new Notification(...)` in there is not an error, it is silence, which is why a
     * backgrounded APK showed nothing for a DM that had already arrived on its open relay socket.
     */
    public static void show(Context ctx, String title, String body, String type, String tag) {
        show(ctx, title, body, type, tag, "notifications");
    }

    public static void show(Context ctx, String title, String body, String type, String tag, String route) {
        boolean isCall = "call".equals(type);
        ensureChannels(ctx);

        Intent open = openIntent(ctx, route, System.currentTimeMillis());
        int flags = PendingIntent.FLAG_UPDATE_CURRENT
                | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0);
        PendingIntent pi = PendingIntent.getActivity(ctx,
                isCall ? 1 : 2000 + Math.abs(String.valueOf(route).hashCode() % 100000), open, flags);

        Notification.Builder b = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(ctx, isCall ? CH_CALLS : CH_MSGS)
                : new Notification.Builder(ctx);
        b.setContentTitle(title)
         .setContentText(body)
         .setSmallIcon(android.R.drawable.sym_action_chat)
         .setAutoCancel(true)
         .setContentIntent(pi);
        if (isCall) {
            // Ringing behaviour: stays until acted on, and asks to take over the screen the way a
            // call should. On Android 14+ the full-screen intent is honoured for calling apps and
            // quietly downgraded to a heads-up otherwise — a downgrade, never a crash.
            b.setOngoing(false);
            b.setCategory(Notification.CATEGORY_CALL);
            b.setFullScreenIntent(pi, true);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                b.setPriority(Notification.PRIORITY_HIGH);
            }
        }
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm != null) {
            // Collapse a repeat of the same kind rather than stacking: a call that re-notifies should
            // replace its own notification, not add a second one to the shade.
            // A TAG lets distinct conversations coexist; without one, everything of a kind
            // collapses onto one id, which is right for "you have mail" and wrong for two people
            // messaging you at once.
            nm.notify(tag != null ? tag : (isCall ? "call" : "msg"), isCall ? 1001 : 1002, b.build());
        }
    }

    /** Package-visible so the device suite can prove the notification's exact deep-link survives. */
    static Intent openIntent(Context ctx, String route, long at) {
        Intent open = new Intent(ctx, MainActivity.class);
        open.setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        open.putExtra(place.poster.app.home.HomeActivity.EXTRA_VIEW,
                route == null || route.trim().isEmpty() ? "notifications" : route.trim());
        open.putExtra(place.poster.app.home.HomeActivity.EXTRA_VIEW_AT, at);
        return open;
    }

    static void ensureChannels(Context ctx) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;
        NotificationChannel calls = new NotificationChannel(
                CH_CALLS, "Calls", NotificationManager.IMPORTANCE_HIGH);
        calls.setDescription("Incoming voice and video calls");
        NotificationChannel msgs = new NotificationChannel(
                CH_MSGS, "Messages and mentions", NotificationManager.IMPORTANCE_DEFAULT);
        nm.createNotificationChannel(calls);
        nm.createNotificationChannel(msgs);
    }
}
