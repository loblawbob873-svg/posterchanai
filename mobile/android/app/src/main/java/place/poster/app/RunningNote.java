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
import place.poster.app.signer.SignerRelayService;

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

    /** The one line of text, composed from whatever is up. Public so a test can read the rules. */
    public static String text() {
        boolean signer = SignerRelayService.running;
        boolean stay = StayAwakeService.running;
        if (signer && stay) return signerLine() + " · staying connected";
        if (signer) return signerLine();
        if (stay) return "Staying connected so messages and calls reach you";
        return "Working in the background";
    }

    public static Notification build(Context ctx) {
        int f = PendingIntent.FLAG_UPDATE_CURRENT
                | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0);
        PendingIntent tap = PendingIntent.getActivity(ctx, 0,
                new Intent(ctx, MainActivity.class)
                        .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP), f);

        boolean signer = SignerRelayService.running;
        boolean stay = StayAwakeService.running;
        boolean both = signer && stay;

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
        return b.build();
    }

    /** Re-post after anything that changes the text. Safe from any thread. */
    public static void refresh(Context ctx) {
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;
        try { nm.notify(ID, build(ctx)); } catch (Throwable ignored) { }
    }

    /**
     * True when a service OTHER than the one asking is still foreground, i.e. the shared
     * notification must survive this stop.
     *
     * @param meSigner true when the caller is the signer, false when it is "stay connected"
     */
    public static boolean othersRunning(boolean meSigner) {
        return meSigner ? StayAwakeService.running : SignerRelayService.running;
    }
}
