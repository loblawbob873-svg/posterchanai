package place.poster.app.push;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;

import org.json.JSONObject;
import org.unifiedpush.android.connector.PushService;
import org.unifiedpush.android.connector.data.PushEndpoint;
import org.unifiedpush.android.connector.data.PushMessage;
import org.unifiedpush.android.connector.FailedReason;

import androidx.annotation.NonNull;

import place.poster.app.MainActivity;

/**
 * Where a push actually lands on the packaged app.
 *
 * A SERVICE, not a BroadcastReceiver: connector 3.x replaced MessagingReceiver with PushService, and
 * only the 3.x line is published to Maven Central at these coordinates — 2.x resolves nowhere, which
 * is what broke the first build.
 *
 * The server sends the SAME JSON payload it sends the service worker — {title, body, type} — so there
 * is one notification contract for every transport rather than a second one that can drift out of
 * step with the web client's.
 *
 * `type` decides the treatment: a call is HIGH importance with a full-screen intent so it behaves like
 * an incoming call rather than an email, everything else is an ordinary heads-up. Two channels, so the
 * user can silence chatter without silencing calls — Android only lets them do that per channel.
 */
public class PushEventService extends PushService {

    public static final String PREFS = "pcai_push";
    public static final String KEY_ENDPOINT = "endpoint";
    private static final String CH_CALLS = "pcai_calls";
    private static final String CH_MSGS = "pcai_messages";

    @Override
    public void onMessage(@NonNull PushMessage message, @NonNull String instance) {
        Context ctx = this;
        String title = "PosterChan", body = "New activity", type = "", route = "notifications";
        try {
            JSONObject j = new JSONObject(new String(message.getContent(), "UTF-8"));
            title = j.optString("title", title);
            body = j.optString("body", body);
            type = j.optString("type", "");
            String eid = j.optString("eid", "").trim();
            route = !eid.isEmpty() ? "post:" + eid : j.optString("view", route).trim();
        } catch (Throwable ignored) {
            // A payload we cannot parse is still a signal that SOMETHING happened; showing the
            // default beats swallowing it, which would look exactly like a delivery failure.
        }
        show(ctx, title, body, type, null, route);
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

    /**
     * The distributor issued (or rotated) our endpoint. Stash it — the web layer reads it on next
     * open and registers it with the server, which is the only place that knows our Nostr key.
     *
     * Storing rather than posting directly is deliberate: this fires whether or not the app is in the
     * foreground, and the server call must be signed by the user's key, which lives in the web layer.
     */
    @Override
    public void onNewEndpoint(@NonNull PushEndpoint endpoint, @NonNull String instance) {
        SharedPreferences.Editor e = getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit();
        e.putString(KEY_ENDPOINT, endpoint.getUrl());
        e.apply();
    }

    @Override
    public void onUnregistered(@NonNull String instance) {
        getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().remove(KEY_ENDPOINT).apply();
    }

    @Override
    public void onRegistrationFailed(@NonNull FailedReason reason, @NonNull String instance) {
        // Nothing to show: the user asked for notifications and will be told by the in-app check,
        // which can explain the fix. A toast from a background receiver cannot.
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
