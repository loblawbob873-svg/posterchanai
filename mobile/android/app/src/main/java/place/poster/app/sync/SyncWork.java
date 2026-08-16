package place.poster.app.sync;

import android.content.Context;
import android.content.pm.ServiceInfo;
import android.os.Build;

import androidx.annotation.NonNull;
import androidx.work.ExistingWorkPolicy;
import androidx.work.ForegroundInfo;
import androidx.work.OneTimeWorkRequest;
import androidx.work.OutOfQuotaPolicy;
import androidx.work.WorkManager;
import androidx.work.Worker;
import androidx.work.WorkerParameters;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

import place.poster.app.RunningNote;

/**
 * THE SWEEP WHEN ANDROID WILL NOT LET US START A FOREGROUND SERVICE.
 *
 * WHY THIS EXISTS BESIDE {@link SyncService}, which does the same job. Android 12+ refuses a
 * background foreground-service start outside a short list of exemptions, and the one this clock
 * would rely on — "your app invokes an EXACT alarm" — is the one an inexact
 * {@code setAndAllowWhileIdle} does not buy: an inexact allow-while-idle alarm is temp-allowlisted
 * with foreground services NOT allowed. {@link SyncClock} asks for an exact alarm where the platform
 * will give one, and on Android 13+ that is a permission the user has to grant, so on a stock phone
 * the refusal is not an OEM edge case — it is the normal path.
 *
 * The fallback that shipped for it first was a bare thread, and a bare thread is the ORIGINAL BUG:
 * a receiver's process is cached seconds later, and a cached process on 12+ is frozen, so the sweep
 * stops having moved a handful of files. That is exactly the report this whole change answers, so
 * the fallback must be something that actually holds the process.
 *
 * A JOB DOES. WorkManager's expedited work is not a foreground service on 31+ — it is an expedited
 * job, which carries no background-start restriction at all — and while it runs the process is not
 * cached and therefore not frozen. Below 31 WorkManager promotes it to a foreground service ITSELF,
 * which is allowed because the job, not us, is starting it; {@link #getForegroundInfo} is what it
 * needs to do that, and omitting it throws.
 *
 * IT IS THE SECOND CHOICE, NOT THE FIRST, and the ordering is deliberate: a job is capped at about
 * ten minutes and expedited work has a daily quota, while a foreground service can run a first sync
 * of a real Pictures folder to the end. So the service is tried first and this catches the phones
 * that refuse it. Both are counted, so the panel can say which one this phone actually got.
 *
 * NOT the same thing as {@link SyncCheckWorker}, which holds no key and only notices changes. This
 * one runs the real sweep, under the same conditions and the same key as the service would.
 */
public class SyncWork extends Worker {

    public static final String WORK_NAME = "pc-folder-sync-sweep";

    /** Bounded so a job that outlives its own window cannot wedge WorkManager's thread for ever.
     *  Comfortably past a job's own ~10 minute ceiling; the sweep checkpoints, so being cut off
     *  costs the tail of one pass and not the pass. */
    private static final long MAX_MS = 12 * 60 * 1000L;

    public SyncWork(@NonNull Context ctx, @NonNull WorkerParameters params) { super(ctx, params); }

    /**
     * @return true when the job was accepted. REPLACE, not KEEP: this is only ever enqueued right
     *         after an alarm, so an existing one is either running (and `NativeRunner` will decline
     *         a second sweep anyway) or stale.
     */
    public static boolean start(Context ctx) {
        try {
            OneTimeWorkRequest req = new OneTimeWorkRequest.Builder(SyncWork.class)
                    .setExpedited(OutOfQuotaPolicy.RUN_AS_NON_EXPEDITED_WORK_REQUEST)
                    .build();
            WorkManager.getInstance(ctx.getApplicationContext())
                    .enqueueUniqueWork(WORK_NAME, ExistingWorkPolicy.REPLACE, req);
            return true;
        } catch (Throwable t) {
            return false;
        }
    }

    /**
     * Only reached on API < 31, where WorkManager runs expedited work as a foreground service of its
     * own. It posts {@link RunningNote}'s shared id so this cannot become a second permanent item in
     * the shade — the same rule every background service in this app follows.
     */
    @NonNull
    @Override
    public ForegroundInfo getForegroundInfo() {
        Context ctx = getApplicationContext();
        RunningNote.ensureChannel(ctx);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            return new ForegroundInfo(RunningNote.ID, RunningNote.build(ctx),
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        }
        return new ForegroundInfo(RunningNote.ID, RunningNote.build(ctx));
    }

    @NonNull
    @Override
    public Result doWork() {
        Context ctx = getApplicationContext();
        final CountDownLatch done = new CountDownLatch(1);
        boolean began;
        try {
            began = NativeRunner.tick(ctx, "background job", new Runnable() {
                public void run() { done.countDown(); }
            });
        } catch (Throwable t) {
            return Result.success();     // nothing to retry: the next alarm is the retry
        }
        if (!began) return Result.success();
        SyncClock.onJob();
        try {
            // BLOCKING ON PURPOSE. doWork() returning is what tells WorkManager the job is over, and
            // the job is the only thing holding this process out of the freezer — returning while the
            // sweep thread runs would hand back exactly the guarantee this class exists to provide.
            done.await(MAX_MS, TimeUnit.MILLISECONDS);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
        return Result.success();
    }
}
