package place.poster.app.sms;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

import androidx.core.app.NotificationCompat;
import androidx.core.app.RemoteInput;

import place.poster.app.R;

/**
 * THE NOTIFICATION A TEXT MAKES, with a reply box in it.
 *
 * ITS OWN CHANNEL, `pcai_sms`, and that is not tidiness. This app already has `pcai_calls` (the
 * Nostr incoming-call ringer, IMPORTANCE_HIGH with a full-screen intent) and `pcai_messages`
 * (Nostr DMs). Reusing either would mean somebody silencing their text messages also silences
 * incoming calls — channels are how a person controls this, and there is no way back from a
 * mis-shared one except uninstalling the app.
 *
 * ONE NOTIFICATION PER CONVERSATION, tagged, so four texts from one person replace each other and
 * two people are two notifications. The tag is what keeps this out of the id space the music
 * service, the screen share and the folder sweep already use.
 */
public final class SmsNotifier {

    public static final String CHANNEL = "pcai_sms";
    private static final String TAG_THREAD = "pcai-sms";
    private static final int ID_MMS = 0x5A11;

    private SmsNotifier() { }

    /** Report both the app permission and the text channel; either can silence delivery. */
    public static boolean canNotify(Context ctx) {
        if (Build.VERSION.SDK_INT >= 33 && ctx.checkSelfPermission("android.permission.POST_NOTIFICATIONS")
                != android.content.pm.PackageManager.PERMISSION_GRANTED) return false;
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null || !nm.areNotificationsEnabled()) return false;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = nm.getNotificationChannel(CHANNEL);
            if (channel != null && channel.getImportance() == NotificationManager.IMPORTANCE_NONE) return false;
        }
        return true;
    }

    static void ensureChannel(Context ctx) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null || nm.getNotificationChannel(CHANNEL) != null) return;
        NotificationChannel c = new NotificationChannel(CHANNEL,
                ctx.getString(R.string.sms_channel), NotificationManager.IMPORTANCE_HIGH);
        c.setDescription(ctx.getString(R.string.sms_channel_why));
        c.enableVibration(true);
        c.setShowBadge(true);
        nm.createNotificationChannel(c);
    }

    public static void incoming(Context ctx, String from, String body, long when, long threadId) {
        ensureChannel(ctx);
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;

        String who = PhoneBook.label(ctx, from);

        Intent open = new Intent(ctx, ThreadActivity.class)
                .putExtra(ThreadActivity.EXTRA_THREAD, threadId)
                .putExtra(ThreadActivity.EXTRA_ADDRESS, from)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);

        NotificationCompat.Builder b = new NotificationCompat.Builder(ctx, CHANNEL)
                .setSmallIcon(R.drawable.ic_pc_chat)
                .setContentTitle(who)
                .setContentText(body)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(body))
                .setWhen(when > 0 ? when : System.currentTimeMillis())
                .setShowWhen(true)
                .setCategory(NotificationCompat.CATEGORY_MESSAGE)
                .setPriority(NotificationCompat.PRIORITY_HIGH)
                .setAutoCancel(true)
                .setContentIntent(activity(ctx, open, (int) threadId))
                .addAction(replyAction(ctx, from, threadId))
                .addAction(new NotificationCompat.Action.Builder(R.drawable.ic_pc_check,
                        ctx.getString(R.string.sms_mark_read),
                        broadcast(ctx, SmsActionReceiver.ACTION_MARK_READ, from, threadId)).build());

        nm.notify(TAG_THREAD, (int) threadId, b.build());
    }

    /**
     * The inline reply box.
     *
     * ITS PendingIntent MUST BE MUTABLE, and this is the trap: from Android 12 a PendingIntent needs
     * one of FLAG_IMMUTABLE or FLAG_MUTABLE or the app crashes when the notification is built — and
     * every other PendingIntent in this codebase is correctly IMMUTABLE. This one cannot be. RemoteInput
     * delivers what the person typed by WRITING IT INTO the intent, so an immutable one arrives with
     * an empty reply and the message is silently never sent.
     */
    private static NotificationCompat.Action replyAction(Context ctx, String from, long threadId) {
        RemoteInput input = new RemoteInput.Builder(SmsActionReceiver.KEY_REPLY)
                .setLabel(ctx.getString(R.string.sms_reply))
                .build();
        Intent i = new Intent(ctx, SmsActionReceiver.class)
                .setAction(SmsActionReceiver.ACTION_REPLY)
                .putExtra(SmsActionReceiver.EXTRA_ADDRESS, from)
                .putExtra(SmsActionReceiver.EXTRA_THREAD, threadId);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT
                | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S ? PendingIntent.FLAG_MUTABLE : 0);
        PendingIntent pi = PendingIntent.getBroadcast(ctx, ("r" + threadId).hashCode(), i, flags);
        return new NotificationCompat.Action.Builder(R.drawable.ic_pc_send,
                    ctx.getString(R.string.sms_reply), pi)
                .addRemoteInput(input)
                .setAllowGeneratedReplies(true)
                .build();
    }

    /** Report a carrier download or provider failure instead of silently losing the MMS. */
    public static void mmsError(Context ctx, String text) {
        ensureChannel(ctx);
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;
        nm.notify(TAG_THREAD, ID_MMS, new NotificationCompat.Builder(ctx, CHANNEL)
                .setSmallIcon(R.drawable.ic_pc_warn)
                .setContentTitle(ctx.getString(R.string.sms_mms_title))
                .setContentText(text)
                .setStyle(new NotificationCompat.BigTextStyle().bigText(text))
                .setCategory(NotificationCompat.CATEGORY_MESSAGE)
                .setPriority(NotificationCompat.PRIORITY_DEFAULT)
                .setAutoCancel(true)
                .build());
    }

    /** Take a conversation's notification down — opening it, or replying from the shade. */
    public static void clear(Context ctx, long threadId) {
        try {
            NotificationManager nm =
                    (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) nm.cancel(TAG_THREAD, (int) threadId);
        } catch (Throwable ignored) { }
    }

    private static PendingIntent activity(Context ctx, Intent i, int code) {
        int flags = PendingIntent.FLAG_UPDATE_CURRENT
                | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0);
        return PendingIntent.getActivity(ctx, code, i, flags);
    }

    private static PendingIntent broadcast(Context ctx, String action, String from, long threadId) {
        Intent i = new Intent(ctx, SmsActionReceiver.class).setAction(action)
                .putExtra(SmsActionReceiver.EXTRA_ADDRESS, from)
                .putExtra(SmsActionReceiver.EXTRA_THREAD, threadId);
        int flags = PendingIntent.FLAG_UPDATE_CURRENT
                | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0);
        return PendingIntent.getBroadcast(ctx, (action + threadId).hashCode(), i, flags);
    }
}
