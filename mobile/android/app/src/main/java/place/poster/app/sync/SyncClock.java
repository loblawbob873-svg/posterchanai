package place.poster.app.sync;

import android.app.AlarmManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.SystemClock;

import java.util.concurrent.atomic.AtomicInteger;

/**
 * THE CLOCK THAT RUNS WITH THE SCREEN OFF — and it belongs to folder sync, not to something else.
 *
 * WHY THIS FILE EXISTS AT ALL, which is the whole of "background sync still does not work". The
 * alarm used to live inside {@code StayAwakeService}: the "Stay connected" foreground service, which
 * is OFF BY DEFAULT and is described — correctly — as a fallback for receiving DMs and calls when no
 * push distributor is installed. So on a phone that had never touched that switch there was no clock
 * at all. Every fix that came before this one (the alarm that fires in Doze, the wake lock, its
 * renewal, resumeTimers, and finally a whole sweep engine written in Java so no WebView is needed)
 * was downstream of a tick that was never emitted. Folder sync worked while the screen was on,
 * because the page's own heartbeat ran, and stopped when it went off. Exactly as reported, on two
 * devices, across several rounds of "fixed".
 *
 * A FEATURE MAY NOT DEPEND ON AN UNRELATED SWITCH IN ANOTHER PART OF SETTINGS. That is the same
 * mistake the background signer made — it could not sign because the only thing that ever stored a
 * key was the "Sign for other apps on this phone" toggle — and it is the same fix: the feature asks
 * for what it needs, itself. The clock is armed by {@link FolderSyncPlugin#configure} whenever this
 * account actually syncs a folder, and cancelled when it stops.
 *
 * IT IS AN ALARM, NOT A HANDLER, AND THAT DISTINCTION IS THE FEATURE. {@code Handler.postDelayed}
 * schedules against {@code SystemClock.uptimeMillis()}, which STOPS ADVANCING in deep sleep, so it
 * fires only when something else happens to wake the phone — it looks like a fix and behaves like
 * the bug. {@code setAndAllowWhileIdle} is the one that fires in Doze; Android rate-limits it to
 * roughly once every nine minutes per app, well under the period below.
 *
 * WHY THE PERIOD IS JUST OVER FIFTEEN MINUTES, and it is not a battery number. {@code shouldSync}
 * refuses when less than {@code minIntervalMs} (15 min) has passed and nothing is dirty — and on
 * Android nothing is ever dirty, because SAF gives no watcher. So a ten-minute alarm aliases against
 * that floor into a TWENTY-minute effective period (sweep at 0, refused at 10, runs at 20). Just
 * above the floor means every alarm that fires does the work it woke up for.
 *
 * IT TARGETS A BROADCAST RECEIVER, NOT A SERVICE. The tick has to be able to START a foreground
 * service — that is what holds the process out of the cached-app freezer for the length of a sweep —
 * and Android 12+ refuses a background foreground-service start by throwing. A receiver is where
 * that throw can be CAUGHT and fallen back from; a {@code PendingIntent.getForegroundService} would
 * throw inside the system's delivery instead, which is a crash, not a fallback.
 */
public final class SyncClock {

    private SyncClock() { }

    public static final String ACTION_TICK = "place.poster.app.SYNC_CLOCK_TICK";

    /** Just over the client's 15-minute `minIntervalMs` — see the class note. */
    static final long PERIOD_MS = 16 * 60 * 1000L;

    /** Distinct from StayAwakeService's retired 0x5C12, so an alarm left over from an older build
     *  cannot be cancelled by us and ours cannot be cancelled by it. */
    private static final int REQ = 0x5C13;

    /* WHAT THE PHONE MEASURED. Same reasoning as FolderSyncPlugin's counters, and static for the
     * same reason: the interesting case is the one where nothing was running, so a field on an
     * object that did not exist at the time answers about the wrong thing. `armed` counts scheduling
     * operations, `fired` alarms that came back, `foreground` sweeps that got a foreground service,
     * `refused` the ones Android would not let us start one for (see SyncTickReceiver). */
    private static final AtomicInteger cArmed = new AtomicInteger();
    private static final AtomicInteger cFired = new AtomicInteger();
    private static final AtomicInteger cForeground = new AtomicInteger();
    private static final AtomicInteger cRefused = new AtomicInteger();
    private static final AtomicInteger cJob = new AtomicInteger();
    private static volatile long lastFired = 0, lastArmed = 0;
    private static volatile boolean armed = false;
    private static volatile boolean exact = false;

    /* PERSISTED, because the in-memory flag can only ever answer "yes" to the person asking.
     *
     * The panel is opened from a running page, and a running page has already called configure(),
     * which arms. So `armedThisProcess()` reads true in every case a user can observe — it cannot
     * surface the failure it was added for. What CAN is the wall-clock time the alarm was last
     * scheduled and last DELIVERED, kept across process deaths: "armed 4 min ago, last fired never"
     * is a clock that is not running, and it says so from the first time anybody looks. */
    private static final String PREFS = "pc_sync_clock";

    private static android.content.SharedPreferences prefs(Context ctx) {
        return ctx.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    static PendingIntent pending(Context ctx) {
        // FLAG_IMMUTABLE is not optional — Android 12+ throws when the PendingIntent is built without it.
        int flags = PendingIntent.FLAG_UPDATE_CURRENT
                | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0);
        return PendingIntent.getBroadcast(ctx.getApplicationContext(), REQ,
                new Intent(ctx.getApplicationContext(), SyncTickReceiver.class).setAction(ACTION_TICK),
                flags);
    }

    /**
     * Schedule the next tick. Idempotent — {@code FLAG_UPDATE_CURRENT} replaces the pending alarm
     * rather than adding a second one, so calling this on every {@code configure()} (which the page
     * does on startup and on every sweep) cannot multiply the rate.
     */
    /**
     * EXACT WHERE THE PLATFORM WILL GIVE US ONE, and this is not about punctuality.
     *
     * Android 12+ refuses a background foreground-service start outside a short list of exemptions,
     * and the one this path depends on is *"your app invokes an EXACT alarm"*. An INEXACT
     * allow-while-idle alarm is temp-allowlisted with foreground services explicitly NOT allowed —
     * so with the inexact form the sweep's service start is refused on essentially every tick, and
     * the whole point of having a service (holding the process out of the freezer) is lost. Sixteen
     * minutes either way is the same clock; the exemption is the difference.
     *
     * It is asked for, never demanded: `canScheduleExactAlarms()` is true by default on 12, and on
     * 13+ it is a permission the user must grant, so most phones fall back to inexact — which is why
     * {@link SyncWork} exists as the second route rather than as a nicety.
     */
    public static void arm(Context ctx) {
        Context app = ctx.getApplicationContext();
        AlarmManager am = (AlarmManager) app.getSystemService(Context.ALARM_SERVICE);
        if (am == null) return;
        long at = SystemClock.elapsedRealtime() + PERIOD_MS;
        boolean canExact = false;
        try {
            canExact = Build.VERSION.SDK_INT < Build.VERSION_CODES.S || am.canScheduleExactAlarms();
        } catch (Throwable ignored) { }
        try {
            // ELAPSED_REALTIME_WAKEUP: counts through sleep, and wakes the device to deliver. A
            // non-WAKEUP alarm queues until something else wakes the phone, which is the Handler's
            // failure wearing a different name.
            if (Build.VERSION.SDK_INT < Build.VERSION_CODES.M) {
                am.set(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pending(app));
            } else if (canExact) {
                try {
                    am.setExactAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pending(app));
                } catch (SecurityException e) {
                    // The permission moved under us between the check and the call. Not fatal, and
                    // not silent: `exact` is what the panel reads.
                    canExact = false;
                    am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pending(app));
                }
            } else {
                am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pending(app));
            }
            // Counted only where the schedule actually took: an alarm the OS refused must not look
            // like one it accepted and never delivered — opposite problems, opposite fixes.
            cArmed.incrementAndGet();
            lastArmed = System.currentTimeMillis();
            armed = true;
            exact = canExact;
            try { prefs(app).edit().putLong("armedAt", lastArmed).putBoolean("exact", exact).apply(); }
            catch (Throwable ignored) { }
        } catch (Throwable ignored) { }
    }

    /** The account stopped syncing anything: stop waking the phone for it. */
    public static void cancel(Context ctx) {
        Context app = ctx.getApplicationContext();
        // Cleared FIRST and unconditionally. Returning early on a null service left the flag saying
        // "armed" for an account that had stopped syncing — a report of the opposite of the truth.
        armed = false;
        try { prefs(app).edit().putLong("armedAt", 0L).apply(); } catch (Throwable ignored) { }
        AlarmManager am = (AlarmManager) app.getSystemService(Context.ALARM_SERVICE);
        if (am == null) return;
        try { am.cancel(pending(app)); } catch (Throwable ignored) { }
    }

    /** Arm or cancel from one fact: does this device sync anything at all. */
    public static void follow(Context ctx, boolean wanted) {
        if (wanted) arm(ctx); else cancel(ctx);
    }

    /**
     * The same decision taken from what is on DISK rather than from anything a page has said this
     * session — which is the only form available at boot, where no page has run.
     *
     * ONE PLACE, deliberately: the plugin arms this from a live configure() and the boot receiver
     * arms it from cold, and two copies of "is there anything to sync" is how one of them ends up
     * answering yes for a paused folder and waking the phone every sixteen minutes for ever.
     */
    public static void followStore(Context ctx) {
        boolean wanted = false;
        try {
            for (SyncStore.Folder f : new SyncStore(ctx).folders()) {
                if (f.enabled && !f.paused) { wanted = true; break; }
            }
        } catch (Throwable ignored) { }
        follow(ctx, wanted);
    }

    static void onFired(Context ctx) {
        cFired.incrementAndGet();
        lastFired = System.currentTimeMillis();
        try { prefs(ctx).edit().putLong("firedAt", lastFired).apply(); } catch (Throwable ignored) { }
    }

    static void onForeground() { cForeground.incrementAndGet(); }

    static void onForegroundRefused() { cRefused.incrementAndGet(); }

    /** The job route was used instead of the service — see {@link SyncWork}. */
    static void onJob() { cJob.incrementAndGet(); }

    public static int jobCount() { return cJob.get(); }

    public static int armedCount() { return cArmed.get(); }

    public static int firedCount() { return cFired.get(); }

    public static int foregroundCount() { return cForeground.get(); }

    public static int refusedCount() { return cRefused.get(); }

    /** Across process deaths — see the note on {@link #PREFS}. In-memory first so a value written
     *  this session is never behind the stored one. */
    public static long lastFiredAt(Context ctx) {
        if (lastFired > 0) return lastFired;
        try { return prefs(ctx).getLong("firedAt", 0L); } catch (Throwable t) { return 0L; }
    }

    public static long lastArmedAt(Context ctx) {
        if (lastArmed > 0) return lastArmed;
        try { return prefs(ctx).getLong("armedAt", 0L); } catch (Throwable t) { return 0L; }
    }

    /** Whether THIS PROCESS armed it. An alarm outlives the process, so false here means "not since
     *  this process started", never "there is none" — which is why it is named for what it measures.
     *  The honest reading of the clock is {@link #lastArmedAt} against {@link #lastFiredAt}. */
    public static boolean armedThisProcess() { return armed; }

    /** Whether the alarm we last scheduled was an EXACT one — which is what decides whether the
     *  sweep may start a foreground service on Android 12+. See {@link #arm}. */
    public static boolean isExact(Context ctx) {
        if (lastArmed > 0) return exact;
        try { return prefs(ctx).getBoolean("exact", false); } catch (Throwable t) { return false; }
    }
}
