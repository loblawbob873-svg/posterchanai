package place.poster.app.phone;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.telecom.Call;

import androidx.core.app.NotificationCompat;

import place.poster.app.R;
import place.poster.app.sms.PhoneBook;

/**
 * THE RINGER, AND THE ONGOING-CALL NOTIFICATION.
 *
 * TWO CHANNELS, and neither is one this app already has. `pcai_calls` is the Nostr WebRTC ringer and
 * `pcai_ongoing_calls` is its in-call notification — sharing either would mean somebody silencing
 * calls over the mobile network also silences calls over the internet, or the reverse, with no way
 * back except uninstalling the app. Channels are the only control a person has over this.
 *
 * THE FULL-SCREEN INTENT IS NOT DECORATION. `PcInCallService.onCallAdded` also starts the activity
 * directly, and on a locked or dozing phone that start is REFUSED SILENTLY — Android has blocked
 * background activity starts since 10. A full-screen intent is the sanctioned way for a ringing call
 * to take the screen, and without it a phone rings with nothing on it.
 */
public final class InCallNotifier {

    /* `_v2` BECAUSE A CHANNEL'S SETTINGS ARE FIXED ONCE IT EXISTS.
     *
     * Android lets an app create a channel and never change its defaults again -- only the person
     * can, in Settings. The first version of this one had no explicit sound, which means the DEFAULT
     * NOTIFICATION CHIME, and it was created that way on every phone that has run this build. Now
     * that telecom plays the real ringtone again (see IN_CALL_SERVICE_RINGING in the manifest) that
     * chime would sound OVER it. A new id is the only way to ship the corrected defaults. */
    public static final String CHANNEL_RINGING = "pcai_cell_incoming_v2";
    public static final String CHANNEL_ONGOING = "pcai_cell_ongoing";
    /** Outside the ids the music service (4243), screen share (4242), calls (4711) and sync use. */
    private static final int ID = 0x5C40;

    private InCallNotifier() { }

    static void ensureChannels(Context ctx) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;
        if (nm.getNotificationChannel(CHANNEL_RINGING) == null) {
            NotificationChannel c = new NotificationChannel(CHANNEL_RINGING,
                    ctx.getString(R.string.tel_channel_ring), NotificationManager.IMPORTANCE_HIGH);
            c.setDescription(ctx.getString(R.string.tel_channel_ring_why));
            /* SILENT, AND THAT IS NOT THE SAME AS QUIET. The platform rings an incoming call -- it
             * uses the ringtone the owner chose, and it honours Do Not Disturb, the silent switch
             * and any per-contact override. This notification is the UI for that call, not a second
             * announcement of it: left with a sound it plays the default notification chime over
             * the ringtone. Vibration stays, since that is the notification's own half and the
             * platform's ringer vibrates on its own schedule. */
            c.setSound(null, null);
            c.enableVibration(true);
            c.setBypassDnd(true);
            nm.createNotificationChannel(c);
        }
        if (nm.getNotificationChannel(CHANNEL_ONGOING) == null) {
            // LOW and silent: it is a status, not an event. An in-call notification that makes a
            // sound makes it during the call.
            NotificationChannel c = new NotificationChannel(CHANNEL_ONGOING,
                    ctx.getString(R.string.tel_channel_ongoing), NotificationManager.IMPORTANCE_LOW);
            c.setDescription(ctx.getString(R.string.tel_channel_ongoing_why));
            c.setSound(null, null);
            c.enableVibration(false);
            c.setShowBadge(false);
            nm.createNotificationChannel(c);
        }
    }

    public static void show(Context ctx, Call call) {
        ensureChannels(ctx);
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null || call == null) return;

        int state = PcInCallService.stateOf(call);
        boolean ringing = CallRules.canAnswer(state);
        String number = PcInCallService.numberOf(call);
        String who = PhoneBook.label(ctx, number);
        if (who.isEmpty()) who = ctx.getString(R.string.tel_unknown);

        Intent open = new Intent(ctx, InCallActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent screen = PendingIntent.getActivity(ctx, 0, open, immutable());

        NotificationCompat.Builder b = new NotificationCompat.Builder(ctx,
                    ringing ? CHANNEL_RINGING : CHANNEL_ONGOING)
                .setSmallIcon(R.drawable.ic_pc_call)
                .setContentTitle(who)
                .setContentText(ctx.getString(ringing
                        ? R.string.tel_incoming
                        : (CallRules.isPending(state) ? R.string.tel_calling : R.string.tel_in_call)))
                .setCategory(NotificationCompat.CATEGORY_CALL)
                .setPriority(ringing ? NotificationCompat.PRIORITY_HIGH : NotificationCompat.PRIORITY_LOW)
                .setOngoing(true)
                .setContentIntent(screen);

        if (ringing) {
            b.setFullScreenIntent(screen, true);
            b.addAction(R.drawable.ic_pc_call, ctx.getString(R.string.tel_answer),
                        action(ctx, InCallReceiver.ACTION_ANSWER));
            b.addAction(R.drawable.ic_pc_close, ctx.getString(R.string.tel_reject),
                        action(ctx, InCallReceiver.ACTION_REJECT));
        } else {
            b.addAction(R.drawable.ic_pc_close, ctx.getString(R.string.tel_hang_up),
                        action(ctx, InCallReceiver.ACTION_HANG_UP));
        }
        try { nm.notify(ID, b.build()); } catch (Throwable ignored) { }
    }

    /** Redraw for the call currently on top, or take it down when there is none. */
    public static void refresh(Context ctx) {
        PcInCallService s = PcInCallService.INSTANCE;
        Call c = s == null ? null : s.primary();
        if (c == null) clear(ctx); else show(ctx, c);
    }

    public static void clear(Context ctx) {
        try {
            NotificationManager nm =
                    (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) nm.cancel(ID);
        } catch (Throwable ignored) { }
    }

    private static PendingIntent action(Context ctx, String what) {
        Intent i = new Intent(ctx, InCallReceiver.class).setAction(what);
        return PendingIntent.getBroadcast(ctx, what.hashCode(), i, immutable());
    }

    private static int immutable() {
        return PendingIntent.FLAG_UPDATE_CURRENT
             | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0);
    }
}
