package place.poster.app;

import android.app.Application;
import android.util.Log;

/**
 * A BACKGROUND THREAD MUST NOT BE ABLE TO END THE APP, AND FOUR ROUNDS OF FIXES SAY SO.
 *
 * "Folder Sync just crashes the app and returns you to desktop." "If you launch it a few times,
 * android says: there is a bug and it closed." "Folder sync still not even opening on android! then
 * I get the prompt to clear the cache for PosterChan!" Each round found a real unguarded task —
 * NativeRunner's sweep, FolderSyncPlugin.nativeReport, SyncService's start thread, seventeen bridge
 * tasks catching Exception rather than Throwable, then SignerPlugin.status — and each round shipped
 * an APK that still died, because the next unguarded one was in the next file.
 *
 * There are twenty-six background tasks in this app whose bodies are not individually wrapped, and
 * auditing them one at a time is how the last four days went. This is the floor underneath all of
 * them.
 *
 * THE ASYMMETRY IS THE WHOLE DESIGN, and it is not "swallow everything":
 *
 *   * A WORKER thread that throws has lost its own piece of work. Java's default handler ends the
 *     entire process for it, which is a wildly disproportionate answer to a Keystore read failing
 *     or a SAF grant having been revoked. The thread dies, loudly, in the log; the app lives.
 *
 *   * The MAIN thread is different and is deliberately NOT caught. Swallowing there leaves a
 *     Looper whose stack has been unwound mid-dispatch: the process survives with a UI that no
 *     longer draws, no longer responds, and reports nothing — strictly worse than crashing, and
 *     undiagnosable. It goes to the previous handler, which crashes properly and files the trace.
 *
 * This is a NET, never a licence. A task that can throw still gets its own try/catch that ANSWERS
 * its caller — a swallowed exception with no reply leaves a JavaScript promise pending for ever,
 * which on a screen is a spinner that never resolves and is harder to recognise than a crash.
 * tests/test_android_bridge_tasks_cannot_kill_the_app.py keeps demanding those; this only means the
 * one that is missed costs a feature instead of the app.
 */
public class PosterChanApp extends Application {

    @Override
    public void onCreate() {
        super.onCreate();
        final Thread.UncaughtExceptionHandler previous = Thread.getDefaultUncaughtExceptionHandler();
        Thread.setDefaultUncaughtExceptionHandler((thread, error) -> {
            boolean main = thread == Looper_mainThread();
            try {
                Log.e("PosterChan", "uncaught on " + (thread == null ? "?" : thread.getName())
                        + (main ? " (MAIN — crashing)" : " (worker — thread ended, app continues)"), error);
            } catch (Throwable ignored) { }
            if (main && previous != null) previous.uncaughtException(thread, error);
            /* A worker returns here: the thread unwinds and dies, and nothing else happens. */
        });
    }

    /** Kept as a method so the handler above reads as one line and can be stubbed in a test. */
    private static Thread Looper_mainThread() {
        return android.os.Looper.getMainLooper().getThread();
    }
}
