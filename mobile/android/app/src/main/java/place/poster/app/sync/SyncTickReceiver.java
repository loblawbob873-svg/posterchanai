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

    /**
     * NOTHING BELOW THE RE-ARM RUNS ON THE MAIN THREAD, AND THAT IS THE WHOLE POINT OF THIS METHOD.
     *
     * `onReceive` is delivered on the app's MAIN LOOPER. Everything it used to call from here — a
     * battery/network read, `NativeRunner.eligible` (Keystore, provider IPC, a `shouldSync` per
     * folder) — is inter-process communication with system services, and this receiver fires at the
     * one moment those are slowest: the device is DOZING, which is precisely what the alarm is for.
     * A blocked main thread is not a crash. It throws nothing, records nothing, and is caught by no
     * try/catch: the user gets "PosterChan isn't responding", and the system kills the process
     * outright when a receiver overruns its ten seconds — which is an app that "just closes, with
     * nothing". That is the reported symptom, and it is invisible to every exception-shaped
     * instrument in this repo, including the crash log added hours ago.
     *
     * `goAsync()` is the sanctioned way to keep a receiver alive past the return of `onReceive`. It
     * hands back a PendingResult; the broadcast is not complete until `finish()` is called on it, so
     * the process stays out of the cache while the decision is made — and it is made on a thread
     * where blocking costs nobody a frame. `finish()` runs in a `finally`, because a PendingResult
     * that is never finished is its own ANR, ten seconds later, in the system's own bookkeeping.
     *
     * The two SyncClock calls stay: both are a counter and a SharedPreferences write, and the re-arm
     * must not be deferred behind a thread start — it IS the repeat of a one-shot alarm.
     */
    @Override
    public void onReceive(Context ctx, Intent intent) {
        SyncClock.onFired(ctx);
        SyncClock.arm(ctx);                     // re-armed FIRST: a throw below must not end the clock

        final Context app = ctx.getApplicationContext();
        PendingResult pr = null;
        try { pr = goAsync(); } catch (Throwable ignored) { }
        final PendingResult keepAlive = pr;
        Thread t = new Thread(new Runnable() {
            public void run() {
                try { decide(app); }
                catch (Throwable ignored) { }
                finally {
                    if (keepAlive != null) try { keepAlive.finish(); } catch (Throwable ignored) { }
                }
            }
        }, "pc-sync-tick");
        t.setPriority(Thread.NORM_PRIORITY - 1);
        t.start();
    }

    /** Everything the alarm has to work out, none of which may happen on the looper. */
    static void decide(Context ctx) {
        /* "Only when plugged in" / "Wi-Fi only", answered here rather than by waking anything to be
         * told the same thing. A pre-filter only — see FolderSyncPlugin.suppressed. */
        boolean skip = false;
        try { skip = FolderSyncPlugin.suppressed(ctx); } catch (Throwable ignored) { }
        if (skip) return;

        // The page, for the accounts this phone cannot sign for (Amber, a bunker) and for the
        // decisions a background sweep deliberately defers. Costs nothing when there is no page.
        // POSTED TO THE MAIN THREAD: it ends in a bridge call into the WebView, which is the one
        // thing here that must NOT move off the looper.
        try { FolderSyncPlugin.tickOnMain("clock"); } catch (Throwable ignored) { }

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
