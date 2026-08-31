package place.poster.app;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;

import androidx.core.app.NotificationCompat;

import place.poster.app.push.StayAwakeService;
import place.poster.app.push.DirectPushService;
import place.poster.app.signer.SignerRelayService;
import place.poster.app.sync.SyncService;

/**
 * ONE permanent notification, however many background services are up.
 *
 * "2 notifications is bullshit" — and it is, from the only side that matters. The signer and "stay
 * connected" are two services because they do two jobs, but nobody outside this codebase asked for
 * two services; they asked for an app that does not put two permanent items in their shade. Android
 * requires every foreground service to post a notification, so the answer is not fewer
 * notifications, it is fewer NOTIFICATION IDS: two services posting the same id are one item on
 * screen, and the text says which jobs are running.
 *
 * THE ORDERING IS THE WHOLE RISK, and it is why this is one class instead of two copies. Two rules:
 *
 *   * A service going away must NOT use STOP_FOREGROUND_REMOVE while the other one is still up —
 *     that deletes the shared notification out from under a service that is still foreground,
 *     leaving a running background service with nothing on screen (which is the thing the platform
 *     requires and the user is owed). It DETACHes instead, and the survivor re-posts.
 *   * The text is composed from what is ACTUALLY running, so each service sets its `running` flag
 *     BEFORE going foreground. Reading a flag its owner sets afterwards would make the first
 *     notification of every start describe an app in which nothing is running.
 *
 * The legacy ids are cancelled on the way through: an install upgrading from the two-notification
 * build already has 4712 and 4713 on screen, and nothing else would ever clear the one whose owner
 * no longer posts it.
 */
public final class RunningNote {

    /** The shared id. Deliberately one of the two old ones so an upgrading install replaces rather
     *  than adds — the other is cancelled in {@link #ensureChannel}. */
    public static final int ID = 4712;
    public static final String CHANNEL = "pcai_running";

    private static final int LEGACY_SIGNER_ID = 4713;
    private static final String LEGACY_SIGNER_CHANNEL = "pcai_signer";
    private static final String LEGACY_STAY_CHANNEL = "pcai_stay_connected";

    private RunningNote() { }

    public static void ensureChannel(Context ctx) {
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;
        // Left over from the two-notification build: the signer's own item, which nothing posts any
        // more and therefore nothing would ever remove.
        try { nm.cancel(LEGACY_SIGNER_ID); } catch (Throwable ignored) { }
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        if (nm.getNotificationChannel(CHANNEL) == null) {
            NotificationChannel ch = new NotificationChannel(CHANNEL, "Running in the background",
                    NotificationManager.IMPORTANCE_MIN);
            ch.setDescription("The permanent notification Android requires while this app keeps "
                            + "working with the screen off — signing for other apps, or staying "
                            + "connected for messages and calls.");
            ch.setShowBadge(false);
            ch.setSound(null, null);
            nm.createNotificationChannel(ch);
        }
        // The two old channels would otherwise sit in the app's notification settings for ever,
        // offering switches that control nothing.
        for (String dead : new String[]{LEGACY_SIGNER_CHANNEL, LEGACY_STAY_CHANNEL}) {
            try { nm.deleteNotificationChannel(dead); } catch (Throwable ignored) { }
        }
    }

    /** What the signer half of the line says. Mirrors what its own notification used to say. */
    private static String signerLine() {
        int apps = SignerRelayService.paired;
        if (apps <= 0) return "No apps paired";
        if (SignerRelayService.connected <= 0) return "Reconnecting…";
        return apps == 1 ? "Signing for 1 app" : "Signing for " + apps + " apps";
    }

    /** The one line of text, composed from whatever is up. Public so a test can read the rules.
     *
     *  COMPOSED, NOT ENUMERATED. It used to be a truth table over two services, which is readable at
     *  two and is four branches at three — and the branch that gets forgotten is always the new
     *  service's, which then runs with a notification describing somebody else's job. */
    public static String text() {
        StringBuilder b = new StringBuilder();
        if (SignerRelayService.running) b.append(signerLine());
        if (StayAwakeService.running) {
            if (b.length() > 0) b.append(" · staying connected");
            else b.append("Staying connected so messages and calls reach you");
        }
        if (DirectPushService.running) {
            if (b.length() > 0) b.append(DirectPushService.connected
                    ? " · notifications connected" : " · reconnecting notifications");
            else b.append(DirectPushService.connected
                    ? "Connected for messages and calls" : "Reconnecting notifications…");
        }
        if (SyncService.running) {
            if (b.length() > 0) b.append(" · syncing folders");
            else b.append("Syncing your folders");
        }
        return b.length() == 0 ? "Working in the background" : b.toString();
    }

    public static Notification build(Context ctx) {
        int f = PendingIntent.FLAG_UPDATE_CURRENT
                | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0);
        PendingIntent tap = PendingIntent.getActivity(ctx, 0,
                new Intent(ctx, MainActivity.class)
                        .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP), f);

        boolean signer = SignerRelayService.running;
        boolean stay = StayAwakeService.running;
        boolean direct = DirectPushService.running;
        boolean both = (signer ? 1 : 0) + (stay ? 1 : 0) + (direct ? 1 : 0) > 1;
        /* The sweep deliberately gets NO action button. The other two are standing preferences the
         * user turned on and may want off from the shade; this one is a few minutes of work that
         * ends by itself, and a "Turn off" beside it would mean "abandon this sweep", which is not
         * a thing anybody wants to be offered at a glance. */

        NotificationCompat.Builder b = new NotificationCompat.Builder(ctx, CHANNEL)
                .setContentTitle("PosterChan")
                .setContentText(text())
                // MINIMUM priority and no badge: a receipt for a setting, not news. Someone who has
                // opted into a permanent notification should be able to forget it is there.
                .setPriority(NotificationCompat.PRIORITY_MIN)
                .setSmallIcon(R.mipmap.ic_launcher)
                .setOngoing(true)
                .setShowWhen(false)
                .setContentIntent(tap);

        /* One switch each, and they have to be NAMED once there are two of them: a bare "Turn off"
         * beside another bare "Turn off" is a coin toss, and the two do very different things. */
        if (signer) {
            PendingIntent off = PendingIntent.getService(ctx, 1,
                    new Intent(ctx, SignerRelayService.class).setAction(SignerRelayService.ACTION_STOP), f);
            b.addAction(0, both ? "Stop signing" : "Turn off", off);
        }
        if (stay) {
            PendingIntent off = PendingIntent.getService(ctx, 2,
                    new Intent(ctx, StayAwakeService.class).setAction(StayAwakeService.ACTION_STOP), f);
            b.addAction(0, both ? "Stop staying connected" : "Turn off", off);
        }
        if (direct) {
            PendingIntent off = PendingIntent.getService(ctx, 4,
                    new Intent(ctx, DirectPushService.class).setAction(DirectPushService.ACTION_STOP), f);
            b.addAction(0, both ? "Stop notifications" : "Turn off", off);
        }
        return b.build();
    }

    /** Re-post after anything that changes the text. Safe from any thread. */
    public static void refresh(Context ctx) {
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;
        try { nm.notify(ID, build(ctx)); } catch (Throwable ignored) { }
    }

    /** Who is asking. A boolean answered this while there were exactly two services; a third made
     *  "the other one" meaningless, which is how a shared notification gets deleted from under a
     *  service that is still foreground. */
    public static final int SIGNER = 1, STAY = 2, SYNC = 3, DIRECT = 4;

    /**
     * True when a service OTHER than the one asking is still foreground, i.e. the shared
     * notification must survive this stop.
     *
     * @param me one of {@link #SIGNER}, {@link #STAY}, {@link #SYNC}
     */
    public static boolean othersRunning(int me) {
        boolean signer = me != SIGNER && SignerRelayService.running;
        boolean stay = me != STAY && StayAwakeService.running;
        boolean sync = me != SYNC && SyncService.running;
        boolean direct = me != DIRECT && DirectPushService.running;
        return signer || stay || sync || direct;
    }
}
