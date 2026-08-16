package place.poster.app.sync;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * Where the alarm lands. Re-arm, decide, and hand the work to something that can hold the process
 * open for the length of it.
 *
 * THE ORDER MATTERS AND IS NOT AESTHETIC. The alarm is re-armed FIRST, before anything that can
 * throw: {@code setAndAllowWhileIdle} is one-shot, so re-arming here IS the repeat, and a throw
 * below it would end the clock for the life of the install — silently, which is the failure mode
 * this whole path exists to get out of.
 *
 * BOTH ENGINES ARE ASKED, EVERY TIME, and the per-folder claim lock is what makes that safe.
 * {@link NativeRunner} does the transfer itself where the account key is on this device; the page is
 * asked as well, because a folder holding one conflict is DEFERRED by the native sweep on every run
 * and only the page can settle it. Skipping the page whenever the native path started reads as an
 * optimisation and is a way to lose sync entirely — {@code tick()} can only ever answer "a thread
 * was spawned", never "the work happened".
 *
 * A RECEIVER GETS ABOUT TEN SECONDS AND THEN THE PROCESS IS CACHED — and a cached process is frozen,
 * so a sweep started on a bare thread here stops a few seconds after the screen goes off, having
 * moved a handful of files. That is precisely the reported symptom. So the sweep runs inside
 * {@link SyncService}, a foreground service, which is what every sync app on Android does and what
 * keeps the process out of the freezer for the minutes a sweep takes.
 *
 * AND WHEN ANDROID REFUSES THE FOREGROUND SERVICE, THE FALLBACK MUST ALSO HOLD THE PROCESS.
 * Android 12+ throws {@code ForegroundServiceStartNotAllowedException} for a background start
 * outside its exemptions, and the exemption this path wants — "your app invokes an EXACT alarm" —
 * is one an INEXACT {@code setAndAllowWhileIdle} does not buy. {@link SyncClock} asks for an exact
 * alarm where the platform gives one, and on Android 13+ that is a user-granted permission, so the
 * refusal is the ORDINARY case on a stock phone, not an OEM quirk. Falling back to a bare thread
 * would be falling back to the original bug — the process is cached seconds later and frozen — so
 * the fallback is {@link SyncWork}: an expedited job, which carries no background-start restriction
 * and keeps the process out of the freezer while it runs. Every route is COUNTED, so the panel can
 * say which one this phone actually got instead of reporting silence.
 */
public class SyncTickReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context ctx, Intent intent) {
        SyncClock.onFired(ctx);
        SyncClock.arm(ctx);                     // re-armed FIRST: a throw below must not end the clock

        /* "Only when plugged in" / "Wi-Fi only", answered here rather than by waking anything to be
         * told the same thing. A pre-filter only — see FolderSyncPlugin.suppressed. */
        boolean skip = false;
        try { skip = FolderSyncPlugin.suppressed(ctx); } catch (Throwable ignored) { }
        if (skip) return;

        // The page, for the accounts this phone cannot sign for (Amber, a bunker) and for the
        // decisions a background sweep deliberately defers. Costs nothing when there is no page.
        try { FolderSyncPlugin.tick("clock"); } catch (Throwable ignored) { }

        boolean due = false;
        try { due = NativeRunner.eligible(ctx); } catch (Throwable ignored) { }
        if (!due) return;

        boolean started = false;
        try { started = SyncService.start(ctx); } catch (Throwable ignored) { }
        if (started) { SyncClock.onForeground(); return; }

        SyncClock.onForegroundRefused();
        // The job route. Not "better than nothing" — it is the one that works on a phone that has
        // not granted the exact-alarm permission, which is most of them from Android 13 on.
        boolean queued = false;
        try { queued = SyncWork.start(ctx); } catch (Throwable ignored) { }
        if (queued) return;

        // Neither route was available. A bare thread is the pre-fix behaviour and will be frozen
        // with the process, but it is the only thing left and it sometimes finishes a small folder.
        try { NativeRunner.tick(ctx, "clock (no service, no job)"); } catch (Throwable ignored) { }
    }
}
