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
    private static final String CH_MSGS_PLAIN = "pcai_messages";
    private static final String SOUND_PREFS = "pc_sounds", K_ALERT = "posterchanAlert", K_SOUND = "arrivalSound";

    /** The account's App arrival sound as this phone last heard it; the chime until told otherwise (as on the web). */
    public static String soundChoice(Context ctx) {
        try {
            android.content.SharedPreferences p = ctx.getSharedPreferences(SOUND_PREFS, Context.MODE_PRIVATE);
            String s = p.getString(K_SOUND, null);
            if (s != null && !s.isEmpty()) return s;
            return p.getBoolean(K_ALERT, false) ? "posterchan" : "chime";   // a phone set up before this was stored
        } catch (Throwable t) { return "chime"; }
    }

    public static void setSoundChoice(Context ctx, String sound) {
        String s = sound == null || sound.trim().isEmpty() ? "chime" : sound.trim();
        ctx.getSharedPreferences(SOUND_PREFS, Context.MODE_PRIVATE).edit()
                .putString(K_SOUND, s).putBoolean(K_ALERT, "posterchan".equals(s)).apply();
    }

    public static boolean alertSoundOn(Context ctx) { return "posterchan".equals(soundChoice(ctx)); }

    public static void setAlertSound(Context ctx, boolean on) { setSoundChoice(ctx, on ? "posterchan" : "chime"); }

    /** The messages channel for the arrival sound: the Alert, the cyberpunk chime, or the phone's own. */
    private static String msgsChannel(Context ctx) {
        return place.poster.app.ringtone.RingtoneRules.messagesChannel(soundChoice(ctx));
    }

    private PushEventService() { }

    /**
     * Render the compact JSON contract delivered by PosterChan Direct. Keeping parsing here means a
     * foreground relay notification and a notification raised by the visible client still use the
     * exact same channels, de-duplication tags and deep links.
     */
    public static boolean deliver(Context ctx, String payload) {
        String title = "PosterChan", body = "New activity", type = "", route = "notifications";
        String wid = "";
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
            wid = j.optString("wid", "");            // dedup key only — never a route
            String eid = j.optString("eid", "").trim();
            /* AN EXPLICIT TAG WINS OVER THE EVENT ID, because some notifications deliberately share
             * an identity with one the CLIENT raises. A DM push sends `pc-dm` — the tag the client's
             * own decrypted "Alice sent you a DM" carries — so the named notification REPLACES the
             * blind "Someone sent you a message" instead of landing beside it. A gift wrap has no
             * `eid` to key on anyway, which is why every DM used to post under the shared "msg". */
            String want = j.optString("tag", "").trim();
            eventTag = !want.isEmpty() ? want : (!eid.isEmpty() ? "nostr-" + eid : null);
            route = !eid.isEmpty() ? "post:" + eid : j.optString("view", route).trim();
        } catch (Throwable ignored) {
            // A payload we cannot parse is still a signal that SOMETHING happened; showing the
            // default beats swallowing it, which would look exactly like a delivery failure.
        }
        // PosterChan Direct and the visible WebView relay can observe the SAME Nostr event. Both use
        // the event-id tag, so Android replaces the duplicate card while distinct events coexist.
        try {
            if (!canNotify(ctx, "call".equals(type))) return false;
            /* THIS PHONE'S OWN ANSWER, checked even though the server already filtered.
             *
             * The server can only filter once the client has managed to mirror the toggles to it,
             * and that call can fail — offline, a refused signature, a reinstall that left a stale
             * row. Nothing about that failure is visible: the tab shows likes off and the phone
             * keeps buzzing for likes, which is exactly the complaint this feature answers. Return
             * TRUE, not false: the notification was handled, not dropped, so a durable delivery is
             * acknowledged rather than retried forever.
             */
            if (!DirectPushStore.allowsType(ctx, type)) return true;
            /* THE CLIENT ALREADY SAID IT, BETTER. This push cannot decrypt a gift wrap, so it says
             * "Someone"; a running client can, and says who. Two notifications for one message.
             * A live client that has just drawn its own is recorded in ClientNotified, and that
             * window is the only thing that can suppress this — no record means SHOW, because a
             * phone whose WebView is not running is exactly the phone this push exists for.
             * Returns TRUE: handled, not dropped, so a durable delivery is acknowledged rather
             * than retried for ever. */
            /* A MESSAGE THIS DEVICE SENT IS NOT NEWS TO IT. NIP-17 publishes a SELF-COPY gift wrap
             * alongside the peer's, so the sender's other devices see what they sent — and its
             * recipient is the sender, which the server cannot detect because a wrap's author is an
             * ephemeral key. Matched on the wrap's own id, so nothing else is ever suppressed. */
            if ("dm".equals(type) && ClientNotified.weSent(wid, System.currentTimeMillis())) return true;
            if ("dm".equals(type) && ClientNotified.recentlyDm(System.currentTimeMillis())) return true;
            show(ctx, title, body, type, eventTag, route);
            return true;
        } catch (Throwable ignored) {
            return false;
        }
    }

    /** A blocked channel must keep the durable notification unacknowledged for retry. */
    public static boolean canNotify(Context ctx, boolean call) {
        if (Build.VERSION.SDK_INT >= 33 && ctx.checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                != android.content.pm.PackageManager.PERMISSION_GRANTED) return false;
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return false;
        if (Build.VERSION.SDK_INT >= 24 && !nm.areNotificationsEnabled()) return false;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = nm.getNotificationChannel(call ? CH_CALLS : msgsChannel(ctx));
            if (channel != null && channel.getImportance() == NotificationManager.IMPORTANCE_NONE) return false;
        }
        return true;
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
                ? new Notification.Builder(ctx, isCall ? CH_CALLS : msgsChannel(ctx))
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
                CH_MSGS_PLAIN, "Messages and mentions", NotificationManager.IMPORTANCE_DEFAULT);
        // The same notifications with the PosterChan Alert sound (User Settings → Sounds). Its own channel,
        // because Android fixes a channel's sound when the channel is created.
        NotificationChannel alert = new NotificationChannel(
                place.poster.app.ringtone.RingtoneRules.messagesChannel(true), "Messages and mentions (PosterChan Alert)",
                NotificationManager.IMPORTANCE_DEFAULT);
        alert.setSound(android.net.Uri.parse("android.resource://" + ctx.getPackageName() + "/" + place.poster.app.R.raw.posterchan_alert),
                new android.media.AudioAttributes.Builder()
                        .setUsage(android.media.AudioAttributes.USAGE_NOTIFICATION)
                        .setContentType(android.media.AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
        // The cyberpunk chime: the arrival sound's DEFAULT on the web and desktop, and PosterChanOS's message
        // sound -- so a phone plays it too unless the person picked something else.
        /* THE PERSON'S OWN SETTINGS CARRY OVER. Every phone moves to this channel by default, and a NEW channel
         * starts at full volume -- so somebody who had silenced or blocked "Messages and mentions" would hear
         * it again after an update (code review). When the chime channel is created for the first time it takes
         * the plain channel's importance (blocked stays blocked) and its silence. Later changes are the person's. */
        String chimeId = place.poster.app.ringtone.RingtoneRules.messagesChannel("chime");
        NotificationChannel had = nm.getNotificationChannel(CH_MSGS_PLAIN);
        boolean fresh = nm.getNotificationChannel(chimeId) == null;
        int importance = NotificationManager.IMPORTANCE_DEFAULT;
        if (fresh && had != null && had.getImportance() != NotificationManager.IMPORTANCE_UNSPECIFIED)
            importance = Math.min(importance, had.getImportance());
        NotificationChannel chime = new NotificationChannel(chimeId, "Messages and mentions (PosterChan chime)", importance);
        if (fresh && had != null && had.getSound() == null) chime.setSound(null, null);
        else chime.setSound(android.net.Uri.parse("android.resource://" + ctx.getPackageName() + "/" + place.poster.app.R.raw.posterchan_chime),
                new android.media.AudioAttributes.Builder()
                        .setUsage(android.media.AudioAttributes.USAGE_NOTIFICATION)
                        .setContentType(android.media.AudioAttributes.CONTENT_TYPE_SONIFICATION).build());
        // "Silent": the notifications still arrive, with no sound.
        NotificationChannel silent = new NotificationChannel(
                place.poster.app.ringtone.RingtoneRules.messagesChannel("off"), "Messages and mentions (silent)",
                NotificationManager.IMPORTANCE_LOW);
        silent.setSound(null, null);
        nm.createNotificationChannel(calls);
        nm.createNotificationChannel(msgs);
        nm.createNotificationChannel(alert);
        nm.createNotificationChannel(chime);
        nm.createNotificationChannel(silent);
    }
}
