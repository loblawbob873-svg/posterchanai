package place.poster.app.sync;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.ContentResolver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.database.Cursor;
import android.net.Uri;
import android.os.Build;
import android.provider.DocumentsContract;

import androidx.annotation.NonNull;
import androidx.core.app.NotificationCompat;
import androidx.work.Constraints;
import androidx.work.ExistingPeriodicWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.PeriodicWorkRequest;
import androidx.work.WorkManager;
import androidx.work.Worker;
import androidx.work.WorkerParameters;

import java.util.ArrayDeque;
import java.util.concurrent.TimeUnit;

/**
 * Background CHANGE DETECTION for folder sync. It notices that there is something to sync; it does
 * not sync.
 *
 * WHY IT CANNOT UPLOAD, which is a fact about the protocol and not a shortcut. Every network step of
 * a sweep is signed by the user's NOSTR key: each Blossom upload carries a kind-24242 auth event,
 * the manifest endpoint takes a kind-27235 proof, and the manifest itself is NIP-44 encrypted to the
 * user's own key. So an unattended uploader needs the nsec — the whole identity, not just the
 * file-encryption key. For anyone signing with Amber or a remote signer (NIP-46) that key is not on
 * the device at all, so for them it is impossible by construction, and this notifier is all there
 * can ever be.
 *
 * FOR A LOCAL KEY THERE IS NOW A SWEEP THAT DOES UPLOAD — {@link NativeRunner}, driven by
 * {@link SyncClock} and running inside {@link SyncService} — and this worker is deliberately not it.
 * The difference is where the key lives: that path reads a secret this app already stored, sealed
 * under an AndroidKeyStore key, on a device the user signed into. This job runs under WorkManager's
 * constraints in a process that may be anything at all, so it keeps its original promise: it holds
 * no key, opens no socket and reads no file content. Both exist because they answer different
 * questions — "sync it" and "tell me there is something to sync".
 *
 * WHAT IT DOES. Under WorkManager's constraints — charging, unmetered, battery not low — it walks
 * each granted tree with the same one-cursor-per-directory scan the plugin uses, hashing nothing,
 * and reduces it to a cheap signature: how many files, and the newest modification time. If that
 * differs from the last signature it told the user about, it posts one notification. Opening the app
 * syncs, because the client already sweeps on launch and on resume.
 *
 * The constraints are the battery story: the OS holds the job until the phone is charging on Wi-Fi
 * and runs it then, so the common case is that this costs nothing at all.
 */
public class SyncCheckWorker extends Worker {

  public static final String WORK_NAME = "pc-folder-sync-check";
  private static final String PREFS = "pc_folder_sync";
  private static final String CHANNEL = "pc-folder-sync";
  private static final int NOTIF_ID = 0x5C11;
  private static final String[] COLS = {
      DocumentsContract.Document.COLUMN_DOCUMENT_ID,
      DocumentsContract.Document.COLUMN_DISPLAY_NAME,
      DocumentsContract.Document.COLUMN_MIME_TYPE,
      DocumentsContract.Document.COLUMN_SIZE,
      DocumentsContract.Document.COLUMN_LAST_MODIFIED,
  };

  public SyncCheckWorker(@NonNull Context ctx, @NonNull WorkerParameters params) { super(ctx, params); }

  /** Idempotent — KEEP, so re-enabling on every app start does not reset the period and starve the
   *  job. Cancelled outright when the user turns it off. */
  public static void schedule(Context ctx, boolean enabled, int minutes) {
    WorkManager wm = WorkManager.getInstance(ctx);
    if (!enabled) { wm.cancelUniqueWork(WORK_NAME); return; }
    int period = Math.max(15, minutes);        // WorkManager's own floor is 15 minutes
    Constraints c = new Constraints.Builder()
        .setRequiresCharging(true)
        .setRequiredNetworkType(NetworkType.UNMETERED)
        .setRequiresBatteryNotLow(true)
        .build();
    wm.enqueueUniquePeriodicWork(WORK_NAME, ExistingPeriodicWorkPolicy.KEEP,
        new PeriodicWorkRequest.Builder(SyncCheckWorker.class, period, TimeUnit.MINUTES)
            .setConstraints(c).build());
  }

  /** Called after a real sweep, so the next check compares against what was actually synced rather
   *  than against the last thing we happened to mention. */
  public static void markSynced(Context ctx) {
    SharedPreferences p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    SharedPreferences.Editor e = p.edit();
    for (android.content.UriPermission up : ctx.getContentResolver().getPersistedUriPermissions()) {
      if (!up.isReadPermission()) continue;
      /* Only FOLDERS. `getPersistedUriPermissions()` also returns grants on single documents the
       * user once picked, and every tree API here throws or answers nothing for those. That is the
       * same list that crashed FolderSyncPlugin.list — see isSyncableTree there. */
      if (!FolderSyncPlugin.isSyncableTree(up.getUri())) continue;
      e.putString("sig:" + up.getUri(), signature(ctx, up.getUri()));
    }
    e.apply();
  }

  @NonNull
  @Override
  public Result doWork() {
    /* A PERIODIC CHECK MUST NOT DIE OF ONE BAD PASS.
     *
     * Everything in here talks to the platform and every one of those calls can throw on a real
     * phone: a SAF grant revoked since the folder was picked answers with SecurityException, and
     * posting the notification needs POST_NOTIFICATIONS on Android 13+, which a person can turn off
     * at any time. WorkManager catches a throwing doWork and marks it FAILED — so this does not end
     * the process, it ends the background checking, quietly and for good, which is the harder
     * failure to notice.
     *
     * `Result.success()` either way is deliberate: this worker only decides whether to post a
     * "something changed" notification, so a pass it could not complete is a pass with nothing to
     * say, not a reason to stop being scheduled. */
    try {
      Context ctx = getApplicationContext();
      SharedPreferences p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
      int changed = 0;
      for (android.content.UriPermission up : ctx.getContentResolver().getPersistedUriPermissions()) {
        if (!up.isReadPermission()) continue;
      /* Only FOLDERS. `getPersistedUriPermissions()` also returns grants on single documents the
       * user once picked, and every tree API here throws or answers nothing for those. That is the
       * same list that crashed FolderSyncPlugin.list — see isSyncableTree there. */
      if (!FolderSyncPlugin.isSyncableTree(up.getUri())) continue;
        String key = "sig:" + up.getUri();
        String now = signature(ctx, up.getUri());
        if (now == null) continue;                     // unreadable this pass — say nothing
        if (!now.equals(p.getString(key, null))) {
          changed++;
          p.edit().putString(key, now).apply();
        }
      }
      if (changed > 0) notifyChanged(ctx, changed);
    } catch (Throwable ignored) { }
    return Result.success();
  }

  /** files + newest mtime. Deliberately cheap: no hashing, no reads, no network. It answers "has
   *  anything moved", which is all a notification needs — the sweep itself works out what. */
  private static String signature(Context ctx, Uri tree) {
    ContentResolver cr = ctx.getContentResolver();
    long count = 0, newest = 0, bytes = 0;
    try {
      ArrayDeque<String> queue = new ArrayDeque<>();
      queue.add(DocumentsContract.getTreeDocumentId(tree));
      int guard = 0;
      while (!queue.isEmpty() && guard++ < 20000) {
        Cursor c = null;
        try {
          c = cr.query(DocumentsContract.buildChildDocumentsUriUsingTree(tree, queue.poll()),
                       COLS, null, null, null);
          if (c == null) continue;
          while (c.moveToNext()) {
            String name = c.getString(1), mime = c.getString(2);
            if (name == null || ".pc-trash".equals(name) || Excludes.isTempName(name)) continue;
            if (DocumentsContract.Document.MIME_TYPE_DIR.equals(mime)) { queue.add(c.getString(0)); continue; }
            count++;
            bytes += c.isNull(3) ? 0 : c.getLong(3);
            long m = c.isNull(4) ? 0 : c.getLong(4);
            if (m > newest) newest = m;
          }
        } finally { if (c != null) c.close(); }
      }
    } catch (Exception e) { return null; }
    return count + ":" + newest + ":" + bytes;
  }

  private static void notifyChanged(Context ctx, int folders) {
    NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
    if (nm == null) return;
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
      NotificationChannel ch = new NotificationChannel(CHANNEL, "Folder sync",
          NotificationManager.IMPORTANCE_LOW);      // LOW: informative, never a sound at 3am
      ch.setDescription("Tells you when a synced folder has changes waiting");
      nm.createNotificationChannel(ch);
    }
    Intent open = ctx.getPackageManager().getLaunchIntentForPackage(ctx.getPackageName());
    if (open == null) return;
    open.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP);
    // FLAG_IMMUTABLE is not optional — Android 12+ throws when the notification is built without it.
    PendingIntent pi = PendingIntent.getActivity(ctx, 0, open,
        PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    String text = folders == 1 ? "A synced folder has changes waiting"
                               : folders + " synced folders have changes waiting";
    Notification n = new NotificationCompat.Builder(ctx, CHANNEL)
        .setSmallIcon(android.R.drawable.stat_sys_upload)
        .setContentTitle("Folder sync")
        .setContentText(text)
        .setSubText("Open PosterChan to sync")     // says WHY tapping helps, since it cannot sync itself
        .setPriority(NotificationCompat.PRIORITY_LOW)
        .setAutoCancel(true)
        .setContentIntent(pi)
        .build();
    try { nm.notify(NOTIF_ID, n); } catch (SecurityException ignored) {}   // notifications denied
  }
}
