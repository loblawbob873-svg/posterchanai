package place.poster.app.sync;

import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;

import androidx.core.app.ServiceCompat;

import place.poster.app.RunningNote;

/**
 * The few minutes a background sweep actually takes, spent somewhere Android will not freeze.
 *
 * WHY A SERVICE, when there is already an alarm, a wake lock and a Java sweep engine. Those three
 * cover the CPU and the clock and cover nothing about the PROCESS: a receiver's ten seconds are up
 * long before a folder is swept, and the moment they are the process is cached — and a cached
 * process on Android 12+ is FROZEN. Threads stop running. Sockets stall. Nothing throws, nothing
 * logs, and the sweep resumes if and only if something else brings the app back. "Not running long
 * in the background, shortly after you turn the screen off" is the shape of that, and it is why
 * every sync app on Android transfers inside a foreground service.
 *
 * IT IS NOT "STAY CONNECTED". That service keeps a WebView and a relay socket alive for as long as
 * the user leaves it on, and costs accordingly; this one exists for the length of one sweep and
 * stops itself. It also does not depend on it: a device that has never turned that switch on gets
 * background folder sync, which is the bug this whole file is the fix for.
 *
 * ONE NOTIFICATION, NOT A SECOND ONE. It posts {@link RunningNote}'s shared id like the other two
 * background services, so a phone that is syncing while the signer is up still shows a single item.
 *
 * `specialUse`, NOT `dataSync`, and that is a deliberate reversal of the obvious answer. From
 * Android 15 an app gets SIX HOURS of dataSync foreground service in any twenty-four, across the
 * whole app, after which the system stops it and refuses further dataSync starts. A first sync of a
 * real Pictures folder is hours of transfer, so the honest type is the one that would silently stop
 * background sync for the rest of the day — on the largest folders, which are exactly the ones that
 * cannot finish while somebody watches. The other two services here made the same trade for the same
 * reason. {@code onTimeout} is still handled: a type that has no timeout today may grow one, and
 * stopping cleanly is never wrong.
 *
 * NOT STICKY. A sweep that died with the process is over; the next alarm is sixteen minutes away and
 * will start a fresh one that re-reads the folder. A STICKY relaunch would restart the service with
 * a null intent and no sweep behind it.
 */
public class SyncService extends Service {

    public static final String ACTION_SWEEP = "place.poster.app.SYNC_SWEEP";

    /** Read by {@link RunningNote} to compose the shared text. Set BEFORE going foreground, or the
     *  first notification of every start describes an app in which this is not running. */
    public static volatile boolean running = false;

    private static volatile boolean sweeping = false;

    /**
     * Ask Android for the service.
     *
     * @return true when the start was accepted. FALSE IS NOT AN ERROR TO SWALLOW: Android 12+ throws
     *         {@code ForegroundServiceStartNotAllowedException} for a background start outside its
     *         exemptions, and the caller falls back to sweeping without one — worse, and not
     *         nothing. Returning a boolean rather than letting it propagate is what makes that
     *         fallback possible at all.
     */
    public static boolean start(Context ctx) {
        Intent i = new Intent(ctx.getApplicationContext(), SyncService.class).setAction(ACTION_SWEEP);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.getApplicationContext().startForegroundService(i);
            } else {
                ctx.getApplicationContext().startService(i);
            }
            return true;
        } catch (Throwable t) {
            return false;
        }
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        RunningNote.ensureChannel(this);
        running = true;
        try {
            int type = Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE
                    ? ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE : 0;
            ServiceCompat.startForeground(this, RunningNote.ID, RunningNote.build(this), type);
        } catch (Throwable t) {
            running = false;   // FIRST, or the shared notification names a service that is not running
            /* THE REFUSAL CAN ARRIVE HERE INSTEAD OF AT THE CALL SITE, and this branch used to give
             * up entirely: no sweep, nothing counted, and the panel still printing "N held the phone
             * awake" because the receiver had already recorded a successful start. Silent total
             * failure dressed as success is the one outcome every rule in this feature forbids. So
             * it is counted as the refusal it is and handed to the job route, exactly as the
             * receiver would have. */
            SyncClock.onForegroundRefused();
            try { SyncWork.start(this); } catch (Throwable ignored) { }
            stopSelf();
            return START_NOT_STICKY;
        }
        /* TWO KINDS OF "ALREADY BUSY", AND THEY NEED OPPOSITE ANSWERS.
         *
         * Android delivers a repeat start to the SAME service instance, so when the sweep in flight
         * is OURS the only correct thing to do is nothing: its `finish()` owns the teardown, and
         * standing down here would take the notification and the process away from a running
         * transfer. When it is somebody ELSE'S sweep — the page's, or the job's — nothing will ever
         * call our `finish()`, so returning would leave this service foreground and the shared
         * notification saying "syncing your folders" until the process died. */
        if (sweeping) return START_NOT_STICKY;
        if (NativeRunner.busy()) { finish(); return START_NOT_STICKY; }
        sweeping = true;
        boolean began = NativeRunner.tick(this, "background clock", new Runnable() {
            public void run() { finish(); }
        });
        if (!began) finish();      // nothing was due after all: stop, do not sit in the shade
        return START_NOT_STICKY;
    }

    /**
     * Called from the sweep thread. Everything happens on the MAIN LOOPER, including clearing
     * `sweeping` — and that is what makes it race-free rather than merely tidy.
     *
     * Clearing the flag here and posting the teardown separately left a window: a start landing
     * between the two saw `sweeping == false`, began a second sweep, and then the posted runnable
     * stopped the service out from under it. `onStartCommand` runs on this same looper, so deciding
     * and tearing down in one runnable means a start either arrives before (sees a sweep in flight
     * and leaves it alone) or after (sees a stopped service and starts a fresh one).
     */
    private void finish() {
        new Handler(Looper.getMainLooper()).post(new Runnable() {
            public void run() {
                sweeping = false;
                dropNotification();
                stopSelf();
            }
        });
    }

    /**
     * Stand down from the shared notification — the same rule the other two services follow.
     *
     * REMOVE would delete it out from under the signer or "stay connected" if either is still up,
     * leaving a running foreground service with nothing in the shade. While anything else needs it
     * we DETACH — the item stays, it just stops being ours — and re-post it without us in the text.
     */
    private void dropNotification() {
        running = false;
        if (RunningNote.othersRunning(RunningNote.SYNC)) {
            ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_DETACH);
            RunningNote.refresh(this);
        } else {
            ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE);
        }
    }

    /**
     * Android's foreground-service time limits. `specialUse` has none today, which is why it is the
     * declared type — but stopping promptly when the platform asks is never wrong, and the
     * alternative is being killed, which mid-transfer is the one thing a sweep handles worse than
     * not running at all.
     */
    @Override
    public void onTimeout(int startId, int fgsType) {
        finish();
    }

    @Override
    public void onDestroy() {
        running = false;
        sweeping = false;
        if (RunningNote.othersRunning(RunningNote.SYNC)) RunningNote.refresh(this);
        super.onDestroy();
    }
}
