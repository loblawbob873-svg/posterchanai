package place.poster.app.sync;

import android.app.Activity;
import android.content.ContentResolver;
import android.content.Context;
import android.content.Intent;
import android.database.Cursor;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.BatteryManager;
import android.provider.DocumentsContract;
import android.util.Base64;

import androidx.activity.result.ActivityResult;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.ActivityCallback;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.util.ArrayList;
import java.util.List;

/**
 * Folder sync on Android — the SAF end of the same two interfaces the desktop bridge implements
 * (see desktop/fsbridge.js). The decision engine and the executor are shared JavaScript; this is
 * only I/O, and the JS shim maps it onto `window.pcFs` so sync.js cannot tell the two apart.
 *
 * SAF, NOT java.io.File. Scoped storage means the app has no path-level access to Pictures or
 * Documents at all; what it can have is a TREE URI the user granted in the system picker, made
 * durable with takePersistableUriPermission so it survives a reboot and an app update. That grant IS
 * the confinement — there is no ".." to defend against here, because there are no paths, only
 * document ids inside a tree the user chose.
 *
 * DO NOT USE DocumentFile. It is the obvious API and it is unusable at this scale: DocumentFile
 * .listFiles() issues a query per child and each getName()/length()/lastModified() is another
 * round trip through the content provider, so a Pictures folder of 20k photos becomes tens of
 * thousands of IPCs. The cursor below asks for every column it needs, for a whole directory, in one
 * query — the difference between a sweep and a phone that gets hot and never finishes.
 *
 * SAF CANNOT SET A FILE'S LAST-MODIFIED TIME. There is no writable COLUMN_LAST_MODIFIED, so the
 * desktop trick of stamping a download with the source mtime is impossible here. That would make
 * every downloaded file look locally-edited on the next sweep — except that write() returns the
 * mtime the provider actually gave the file, and the executor records THAT as the agreed state
 * (syncrun.js `agree(path, {sha, size, mtime: st.mtime})`). The loop closes because the executor
 * believes the filesystem rather than its own intent.
 *
 * THERE IS NO WATCHER, AND THAT IS WHY THE CLOCK IS NATIVE. SAF exposes no reliable change
 * notification for a tree, and polling one is precisely the battery bug the policy exists to avoid,
 * so watch() answers false. That left one automatic trigger, a JS setInterval — which Android
 * throttles in a hidden WebView — so with the screen off the app synced only when opened.
 *
 * Background sync now runs under "Stay connected", whose foreground service keeps the WebView (and
 * therefore the key) resident and arms an AlarmManager alarm that fires in Doze; see tick() above.
 * The UNATTENDED job is still only a notifier (SyncCheckWorker), for the reason it always was: an
 * uploader with no page needs the nsec in native storage, which with Amber or NIP-46 does not exist
 * on the device at all.
 *
 * The battery story is unchanged and is still shouldSync's: "only when plugged in" and "Wi-Fi only"
 * decide in JavaScript, and suppressed() below is a pre-filter that can only skip a tick those were
 * certain to refuse.
 */
@CapacitorPlugin(name = "FolderSync")
public class FolderSyncPlugin extends Plugin {

  /**
   * THE TICK THAT MAKES A BACKGROUNDED SWEEP POSSIBLE AT ALL.
   *
   * With the screen off, the client's only automatic trigger was a JS `setInterval` — and Android
   * throttles timers in a hidden WebView into uselessness, so "Stay connected" kept the process
   * alive and nothing ever asked it to sync. Reported exactly that way: syncing stops every time the
   * screen goes off, with the switch already on.
   *
   * So the CLOCK moves native, the same division of labour the music controls use: the service owns
   * the timer, JS performs the work. This emits an event and nothing else — it holds no key, opens
   * no socket and reads no file (see SyncCheckWorker for why an unattended uploader is impossible);
   * the WebView, which does hold the key, is what sweeps.
   *
   * IT DOES NOT DECIDE WHETHER TO SYNC, and that is why it can be this simple. `shouldSync` still
   * runs on the other side and still declines on battery, on a metered link, or within the minimum
   * interval — which is what the "only when plugged in" and "Wi-Fi only" switches already mean. A
   * tick that arrives when the constraints are not met costs one policy check and nothing else.
   *
   * A dead or absent WebView is not an error: `INSTANCE` is null, the tick is dropped, and the
   * WorkManager job keeps doing the one thing it can do unattended — notice and notify.
   */
  private static volatile FolderSyncPlugin INSTANCE = null;

  /* WHAT THE PHONE ACTUALLY MEASURED, because there is no device here and this failure REPORTS
   * SUCCESS. An alarm that never fires, a tick emitted into a dead bridge and a sweep that ran are
   * indistinguishable from this side — all three look like silence — and the music controls cost
   * four APK builds of guessing before they were made to count instead.
   *
   * STATIC, for the same reason MusicService's counters are: the interesting case is the one where
   * the page was gone, and an instance field would be read off a plugin that did not exist when the
   * tick happened — answering "nothing has ticked this session" about the very tick under
   * investigation.
   *
   * `armed` counts SCHEDULING OPERATIONS, `fired` alarms that came back, `delivered` ticks handed to
   * a live bridge, `dropped` ticks with no bridge to hand them to, `suppressed` ticks the battery
   * pre-filter skipped. `fired > delivered` is a page that keeps dying.
   *
   * DO NOT READ `armed > fired + 1` AS DOZE EATING ALARMS — that was the first reading written here
   * and it is wrong. Only ONE alarm is ever outstanding (`FLAG_UPDATE_CURRENT` replaces the pending
   * one rather than adding to it), while every service start arms again: a STICKY relaunch, a boot,
   * stay-connected off and on. Those are counted by `restarts`, so the honest comparison is
   * `armed - restarts` against `fired`. Without that subtraction a perfectly healthy phone shows the
   * exact signature these counters exist to identify, after two or three service restarts. */
  private static final java.util.concurrent.atomic.AtomicInteger tArmed = new java.util.concurrent.atomic.AtomicInteger();
  private static final java.util.concurrent.atomic.AtomicInteger tFired = new java.util.concurrent.atomic.AtomicInteger();
  private static final java.util.concurrent.atomic.AtomicInteger tDelivered = new java.util.concurrent.atomic.AtomicInteger();
  private static final java.util.concurrent.atomic.AtomicInteger tDropped = new java.util.concurrent.atomic.AtomicInteger();
  private static final java.util.concurrent.atomic.AtomicInteger tRestarts = new java.util.concurrent.atomic.AtomicInteger();
  private static volatile long tLastFired = 0, tLastDelivered = 0;

  public static void onAlarmArmed() { tArmed.incrementAndGet(); }
  /** The service started and armed its first alarm — not a delivery that went missing. */
  public static void onServiceStarted() { tRestarts.incrementAndGet(); }
  public static void onAlarmFired() { tFired.incrementAndGet(); tLastFired = System.currentTimeMillis(); }

  /* THE TWO CHECKBOXES, ANSWERED BEFORE THE WEBVIEW IS WOKEN.
   *
   * `shouldSync` already declines on "only when plugged in" and "Wi-Fi only" — that is where the
   * decision lives and it is not moving. But it lives in JavaScript, so honouring the switches
   * costs a renderer wake-up and a plugin round trip every sixteen minutes, on a phone in a pocket,
   * to arrive at "no" — which is precisely the battery the switches were ticked to save.
   *
   * So the CLIENT pushes what its folders need (`needCharging` if EVERY enabled folder requires a
   * charger, `needUnmetered` likewise) and the alarm checks the phone's own state against it before
   * emitting anything. EVERY, not ANY: one folder willing to run on battery has to be able to, and
   * suppressing on the strictest folder's preference would silently stop the others.
   *
   * IT IS A PRE-FILTER, NEVER THE DECISION. It can only suppress a tick that JavaScript was certain
   * to decline anyway — battery and metered are exactly the two facts both sides read from the same
   * APIs — so a stale or absent policy costs a wasted wake-up, never a missed sync. Default is
   * `false/false`: an APK whose page has not run yet suppresses nothing. */
  private static volatile boolean pNeedCharging = false, pNeedUnmetered = false;
  private static final java.util.concurrent.atomic.AtomicInteger tSuppressed = new java.util.concurrent.atomic.AtomicInteger();

  /** @return true when this tick cannot possibly result in a sweep, so it is not worth a wake-up. */
  public static boolean suppressed(Context ctx) {
    if (!pNeedCharging && !pNeedUnmetered) return false;
    try {
      if (pNeedCharging) {
        BatteryManager bm = (BatteryManager) ctx.getSystemService(Context.BATTERY_SERVICE);
        if (bm != null && !bm.isCharging()) { tSuppressed.incrementAndGet(); return true; }
      }
      if (pNeedUnmetered) {
        ConnectivityManager cm = (ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE);
        NetworkCapabilities nc = cm == null ? null : cm.getNetworkCapabilities(cm.getActiveNetwork());
        // Unknown network is NOT treated as metered: failing closed here would stop background sync
        // outright on any device this read is unreliable on, which is the worse of the two errors.
        if (nc != null && !nc.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED)) {
          tSuppressed.incrementAndGet(); return true;
        }
      }
    } catch (Throwable ignored) {}
    return false;
  }

  /* THE CPU HAS TO BE AWAKE FOR A SWEEP, AND NOTHING WAS KEEPING IT AWAKE.
   *
   * "Stay connected" keeps the PROCESS resident — that is what a foreground service does, and it is
   * why the WebView survives with the app off screen. It does not keep the PROCESSOR running. So
   * with the screen off the device suspends between alarms and the sweep stops mid-file: measured,
   * 23 downloads in the minute before the screen went off and 0 in the minute after. The alarm was
   * never the problem — it fires, the tick is delivered, and then there is no CPU to sweep with.
   *
   * Held ONLY while a sweep is actually running, which is what makes it defensible: `shouldSync` has
   * already decided the folder may run at all (charging, unmetered, not inside the minimum interval),
   * so this cannot keep a phone awake that the user's own switches said no to.
   *
   * THREE THINGS STOP IT LEAKING, because a stuck wake lock is a flat battery and the page holding
   * it is the half Android takes away:
   *   * `acquire(timeout)` — the OS releases it regardless, so a renderer killed mid-sweep costs one
   *     timeout and not the night;
   *   * `setReferenceCounted(false)` — begin/end are called across a bridge that can drop either, and
   *     a counted lock left at +1 by one lost `end` is never released again;
   *   * released in handleOnDestroy, so a page going away takes it with it.
   */
  private static final long WAKE_MAX_MS = 10 * 60 * 1000L;
  private static android.os.PowerManager.WakeLock wake = null;

  @PluginMethod
  public void sweepBegin(PluginCall call) {
    try {
      if (wake == null) {
        android.os.PowerManager pm =
            (android.os.PowerManager) getContext().getSystemService(Context.POWER_SERVICE);
        if (pm == null) { call.resolve(); return; }
        wake = pm.newWakeLock(android.os.PowerManager.PARTIAL_WAKE_LOCK, "posterchan:foldersync");
        wake.setReferenceCounted(false);
      }
      /* RE-ACQUIRE, do not skip when already held. A timed wake lock is not renewed by being held —
       * the OS reclaims it when the timeout expires — so taking it once at the start of a sweep buys
       * exactly WAKE_MAX_MS and then the device suspends mid-file again. Reported as "seems to last
       * longer", which is precisely ten minutes longer.
       *
       * The bound stays: a renderer killed mid-sweep must not keep the processor up all night. It is
       * the SWEEP that renews it, once per file, so the lock outlives a long transfer and dies with
       * a page that stopped asking. acquire() on a non-reference-counted lock resets the timeout. */
      wake.acquire(WAKE_MAX_MS);
      /* AND MAKE SURE JAVASCRIPT IS ACTUALLY RUNNING.
       *
       * A wake lock keeps the CPU up; it does nothing about the WebView. `WebView.pauseTimers()` is
       * APP-WIDE and stops all JavaScript in the process — every timer, every scheduled task — and
       * the activity lifecycle can call it when the app goes to the background. That is exactly the
       * reported shape: sync works for a short period, stops, and starts again the moment the app is
       * opened. A sweep with no JavaScript running is a sweep that does not run, however awake the
       * processor is and however faithfully the alarm fires.
       *
       * `resumeTimers()` is the counter-call and is idempotent: where nothing paused them it is a
       * no-op, so this cannot cost anything on a device that was working. It is scoped to a sweep —
       * taken with the lock, and the sweep is the only thing that asks — so it is not a standing
       * "keep the app awake" flag. */
      try {
        final android.webkit.WebView wv = getBridge() != null ? getBridge().getWebView() : null;
        if (wv != null) getBridge().getActivity().runOnUiThread(() -> {
          try { wv.resumeTimers(); } catch (Throwable ignored) {}
        });
      } catch (Throwable ignored) {}
    } catch (Throwable ignored) {}
    call.resolve();
  }

  @PluginMethod
  public void sweepEnd(PluginCall call) {
    releaseWake();
    call.resolve();
  }

  private static void releaseWake() {
    try { if (wake != null && wake.isHeld()) wake.release(); } catch (Throwable ignored) {}
  }

  /* ---- THE NATIVE SWEEP'S SETTINGS, handed over by the page ------------------------------------
   *
   * Everything the phone needs to sync on its own is something the client already knows and Java
   * cannot work out: the instance URL, the media server, the pair keys, the exclusion lists, the
   * per-folder switches, and the NIP-44-wrapped drive key. Pushed on every sweep and at startup, so
   * a background sweep works from what the user last saw on screen rather than from a stale copy.
   *
   * THE KEY IS STORED WRAPPED — the same value the drive index already holds, unreadable without the
   * account's Nostr secret, which the native signer has anyway. No new secret at rest.
   */
  @PluginMethod
  public void configure(PluginCall call) {
    try {
      JSArray folders = call.getArray("folders");
      new SyncStore(getContext()).configure(
          Boolean.TRUE.equals(call.getBoolean("enabled", false)),
          call.getString("apiBase", ""),
          call.getString("mediaBase", ""),
          call.getString("mkWrapped", ""),
          call.getString("device", ""),
          folders == null ? "[]" : folders.toString());
    } catch (Throwable t) {
      call.reject("could not store the sync settings: " + t.getMessage());
      return;
    }
    call.resolve();
  }

  /** Sign-out, or the switch turned off: the wrapped key must not outlive the session that set it. */
  @PluginMethod
  public void forgetNative(PluginCall call) {
    new SyncStore(getContext()).forget();
    call.resolve();
  }

  /** What the last unattended sweep actually did — the only surface any of it can be read from. */
  @PluginMethod
  public void nativeReport(PluginCall call) {
    SyncStore store = new SyncStore(getContext());
    JSObject o = new JSObject();
    o.put("enabled", store.nativeEnabled());
    o.put("haveKey", place.poster.app.signer.SignerKey.have(getContext()));
    o.put("running", NativeRunner.busy());
    o.put("why", NativeRunner.why());
    o.put("report", store.lastReport());
    call.resolve(o);
  }

  /* ONE SWEEP PER FOLDER, ACROSS BOTH ENGINES. The page can start a sweep at any moment (someone
   * opens the app while the alarm is running one), and two sweeps writing the same manifest is
   * last-writer-wins on the document that decides whether files exist. Both sides take the same
   * lock; a refusal is an ordinary "already syncing", not an error. */
  /* WHAT THIS PAGE IS HOLDING, so it can be given back when the page is taken away. A claim is
   * released in the sweep's `finally`, which does not run when the renderer is killed mid-sweep —
   * the case this whole native path exists for. Without this the key stays claimed for the life of
   * the process and NEITHER engine can touch that folder again until the app is force-stopped. */
  private static final java.util.Set<String> pageClaims =
      java.util.Collections.synchronizedSet(new java.util.LinkedHashSet<String>());

  @PluginMethod
  public void claimSweep(PluginCall call) {
    String key = call.getString("key", "");
    boolean ok = NativeSweep.claim(key);
    if (ok) pageClaims.add(key);
    JSObject o = new JSObject();
    o.put("ok", ok);
    call.resolve(o);
  }

  @PluginMethod
  public void releaseSweep(PluginCall call) {
    String key = call.getString("key", "");
    pageClaims.remove(key);
    NativeSweep.release(key);
    call.resolve();
  }

  @PluginMethod
  public void setTickPolicy(PluginCall call) {
    pNeedCharging = Boolean.TRUE.equals(call.getBoolean("needCharging", false));
    pNeedUnmetered = Boolean.TRUE.equals(call.getBoolean("needUnmetered", false));
    call.resolve();
  }

  @Override
  public void load() {
    INSTANCE = this;
  }

  @Override
  protected void handleOnDestroy() {
    // The page is going. Clearing this is what stops a tick being delivered into a dead bridge —
    // and dropping the wake lock with it, because the only thing that could have released it was
    // the sweep running in the page that just went away.
    if (INSTANCE == this) INSTANCE = null;
    releaseWake();
    /* …and every folder this page had claimed. Only its own: a native sweep running in the service
     * must survive the page dying, which is the entire point of it. */
    synchronized (pageClaims) {
      if (!pageClaims.isEmpty()) {
        NativeSweep.releaseAll(new java.util.LinkedHashSet<String>(pageClaims));
        pageClaims.clear();
      }
    }
    super.handleOnDestroy();
  }

  /**
   * @return true when a live BRIDGE existed to emit into — NOT that anything heard it.
   *
   * Capacitor's `notifyListeners` finds no registered listeners, logs "No listeners found", and
   * returns normally, so this cannot tell a subscribed page from an unsubscribed one. That window is
   * real rather than theoretical: after a renderer kill the page is back long before `startAll()`
   * runs, so there are seconds in which the bridge is live and `fs.onTick` has not been called yet.
   * Nothing today reads this — the caller re-arms its clock either way — and it is written down
   * because the moment something logs or counts on it, the honest answer is the one above.
   */
  public static boolean tick(String why) {
    FolderSyncPlugin p = INSTANCE;
    if (p == null) { tDropped.incrementAndGet(); return false; }
    try {
      JSObject data = new JSObject();
      data.put("why", why == null ? "native" : why);
      p.notifyListeners("folderSyncTick", data);
      tDelivered.incrementAndGet();
      tLastDelivered = System.currentTimeMillis();
      return true;
    } catch (Throwable t) {
      tDropped.incrementAndGet();
      return false;
    }
  }

  /** What the phone measured about the background clock. Read by Folder Sync → "Background details",
   *  which is the only place any of this can be observed — there is no device in the loop here. */
  @PluginMethod
  public void tickStats(PluginCall call) {
    JSObject o = new JSObject();
    o.put("armed", tArmed.get());
    o.put("fired", tFired.get());
    o.put("delivered", tDelivered.get());
    o.put("dropped", tDropped.get());
    o.put("suppressed", tSuppressed.get());
    o.put("restarts", tRestarts.get());
    o.put("needCharging", pNeedCharging);
    o.put("needUnmetered", pNeedUnmetered);
    o.put("lastFiredAt", tLastFired);
    o.put("lastDeliveredAt", tLastDelivered);
    o.put("serviceUp", place.poster.app.push.StayAwakeService.running);
    o.put("stayConnected", place.poster.app.push.StayAwakeService.wanted(getContext()));
    call.resolve(o);
  }

  // ---- roots ------------------------------------------------------------------------------------

  /** The trees the user has granted, straight from the system. Persisted BY ANDROID, not by us — so
   *  there is no local list to drift out of step with what is actually permitted. */
  @PluginMethod
  public void list(PluginCall call) {
    JSArray out = new JSArray();
    for (android.content.UriPermission p : getContext().getContentResolver().getPersistedUriPermissions()) {
      if (!p.isReadPermission()) continue;
      JSObject o = new JSObject();
      o.put("id", p.getUri().toString());
      o.put("dir", prettyName(p.getUri()));
      out.put(o);
    }
    JSObject ret = new JSObject();
    ret.put("roots", out);
    call.resolve(ret);
  }

  @PluginMethod
  public void pick(PluginCall call) {
    Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
    i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
             | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
             | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
    startActivityForResult(call, i, "picked");
  }

  @ActivityCallback
  private void picked(PluginCall call, ActivityResult result) {
    if (call == null) return;
    if (result.getResultCode() != Activity.RESULT_OK || result.getData() == null
        || result.getData().getData() == null) {
      call.resolve(new JSObject());   // cancelled — not an error
      return;
    }
    Uri tree = result.getData().getData();
    // Persist it, or the grant dies with the process and the folder silently stops syncing after a
    // reboot with nothing to say why.
    try {
      getContext().getContentResolver().takePersistableUriPermission(tree,
          Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
    } catch (SecurityException e) {
      call.reject("could not keep access to that folder: " + e.getMessage());
      return;
    }
    JSObject o = new JSObject();
    o.put("id", tree.toString());
    o.put("dir", prettyName(tree));
    call.resolve(o);
  }

  @PluginMethod
  public void forget(PluginCall call) {
    String id = call.getString("id", "");
    try {
      getContext().getContentResolver().releasePersistableUriPermission(Uri.parse(id),
          Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
    } catch (Exception ignored) {}
    call.resolve();
  }

  // ---- scanning / reading / writing --------------------------------------------------------------
  //
  // ALL OF IT NOW LIVES IN SafFs, and these are delegations rather than implementations. It moved
  // because a background sweep has no WebView to call a @PluginMethod from — see NativeSweep — and
  // two copies of "move this file to the trash" is exactly the shape of duplication this feature
  // cannot survive. The behaviour of every method below is unchanged; the code is one file over.

  private SafFs fs(String id) { return new SafFs(getContext(), id); }

  private static JSObject mapToJs(java.util.Map<String, Object> m) {
    JSObject o = new JSObject();
    for (java.util.Map.Entry<String, Object> e : m.entrySet()) o.put(e.getKey(), e.getValue());
    return o;
  }

  @PluginMethod
  public void scan(PluginCall call) {
    final String id = call.getString("id", "");
    final boolean hash = Boolean.TRUE.equals(call.getBoolean("hash", false));
    final long maxBytes = call.getLong("maxBytes", 0L) == null ? 0L : call.getLong("maxBytes", 0L);
    final List<String> excludes = strings(call.getArray("excludes"));

    // Off the WebView thread: a Pictures folder is minutes of provider queries, and blocking here
    // freezes the UI the user is watching the progress in.
    getBridge().execute(() -> {
      try {
        SafFs.Scan sc = fs(id).scan(hash, maxBytes, excludes);
        JSObject files = new JSObject();
        for (java.util.Map.Entry<String, java.util.Map<String, Object>> e : sc.files.entrySet()) {
          files.put(e.getKey(), mapToJs(e.getValue()));
        }
        JSArray skipped = new JSArray();
        for (java.util.Map<String, Object> s : sc.skipped) skipped.put(mapToJs(s));
        JSObject ret = new JSObject();
        ret.put("files", files);
        ret.put("skipped", skipped);
        call.resolve(ret);
      } catch (Exception e) {
        call.reject("scan failed: " + e.getMessage());
      }
    });
  }

  @PluginMethod
  public void read(PluginCall call) {
    final String id = call.getString("id", ""), rel = call.getString("rel", "");
    getBridge().execute(() -> {
      try {
        JSObject ret = new JSObject();
        ret.put("b64", Base64.encodeToString(fs(id).readAll(rel), Base64.NO_WRAP));
        call.resolve(ret);
      } catch (Exception e) { call.reject("read failed: " + e.getMessage()); }
    });
  }

  /* ---- SLICE I/O: a file too big to hold in one piece -----------------------------------------
   *
   * read()/write() move a whole file through one base64 string, so a 200 MB video is that much in
   * the plugin, again across the bridge, and again in the WebView — where it is then encrypted,
   * making three or four copies of the file in a process with far less headroom than a desktop.
   * Android simply died. These move one chunk at a time instead, so the ceiling stops depending on
   * the size of the file.
   */
  @PluginMethod
  public void readPart(PluginCall call) {
    final String id = call.getString("id", ""), rel = call.getString("rel", "");
    final long off = call.getLong("offset", 0L);
    final int len = call.getInt("len", 0);
    getBridge().execute(() -> {
      try {
        byte[] got = fs(id).readRange(rel, off, len);
        JSObject ret = new JSObject();
        ret.put("b64", Base64.encodeToString(got, Base64.NO_WRAP));
        ret.put("len", got.length);
        call.resolve(ret);
      } catch (Exception e) { call.reject("readPart failed: " + e.getMessage()); }
    });
  }

  @PluginMethod
  public void writePart(PluginCall call) {
    final String id = call.getString("id", ""), rel = call.getString("rel", "");
    final String b64 = call.getString("b64", "");
    final long off = call.getLong("offset", 0L);
    getBridge().execute(() -> {
      try {
        fs(id).writePart(rel, off, Base64.decode(b64, Base64.DEFAULT));
        call.resolve(new JSObject());
      } catch (Exception e) { call.reject("writePart failed: " + e.getMessage()); }
    });
  }

  /* THE SAME THREE THE DESKTOP EXPOSES, so a download is verified HERE too.
   *
   * syncrun skips the checksum check entirely when the adapter has no hashPart — a deliberate escape
   * hatch for older shells — so without these the phone and the tablet wrote every download
   * unverified while the laptop checked every one. And since resume is only allowed where the result
   * can be checked, Android also re-downloaded from byte zero after any drop.
   */
  @PluginMethod
  public void hashPart(PluginCall call) {
    final String id = call.getString("id", ""), rel = call.getString("rel", "");
    getBridge().execute(() -> {
      try {
        String sha = fs(id).hashPart(rel);
        if (sha == null) { call.reject("could not read the part file for " + rel); return; }
        JSObject ret = new JSObject();
        ret.put("sha", sha);
        call.resolve(ret);
      } catch (Exception e) { call.reject("hashPart failed: " + e.getMessage()); }
    });
  }

  @PluginMethod
  public void discardPart(PluginCall call) {
    final String id = call.getString("id", ""), rel = call.getString("rel", "");
    getBridge().execute(() -> {
      try {
        fs(id).discardPart(rel);
        call.resolve(new JSObject());
      } catch (Exception e) { call.reject("discardPart failed: " + e.getMessage()); }
    });
  }

  /** How much of an interrupted download is already on disk. 0 when there is nothing to resume. */
  @PluginMethod
  public void partSize(PluginCall call) {
    final String id = call.getString("id", ""), rel = call.getString("rel", "");
    getBridge().execute(() -> {
      try {
        JSObject ret = new JSObject();
        ret.put("size", fs(id).partSize(rel));
        call.resolve(ret);
      } catch (Exception e) { call.reject("partSize failed: " + e.getMessage()); }
    });
  }

  /** Put the finished part file in place — the tail of write(), reused so both paths land the same. */
  @PluginMethod
  public void writeCommit(PluginCall call) {
    final String id = call.getString("id", ""), rel = call.getString("rel", "");
    final Long when = call.getLong("when", 0L);
    getBridge().execute(() -> {
      try {
        long[] st = fs(id).commitPart(rel, when == null ? 0L : when);
        JSObject ret = new JSObject();
        ret.put("size", st[0]);
        ret.put("mtime", st[1]);
        call.resolve(ret);
      } catch (Exception e) { call.reject(e.getMessage()); }
    });
  }

  /**
   * Write, as close to atomically as SAF allows.
   *
   * There is no rename-over-an-existing-document: renameDocument fails if the name is taken. So the
   * bytes go to `name.pcpart` first, any existing document is moved into the trash rather than
   * deleted, and only then is the part renamed into place.
   */
  @PluginMethod
  public void write(PluginCall call) {
    final String id = call.getString("id", ""), rel = call.getString("rel", "");
    final String b64 = call.getString("b64", "");
    final Long when = call.getLong("when", 0L);
    getBridge().execute(() -> {
      try {
        long[] st = fs(id).write(rel, Base64.decode(b64, Base64.DEFAULT), when == null ? 0L : when);
        // The provider decides the mtime — SAF has no writable last-modified — so report what it
        // actually became. syncrun.js records THIS as the agreed state, which is what stops the next
        // sweep reading our own download as a local edit.
        JSObject ret = new JSObject();
        ret.put("size", st[0]);
        ret.put("mtime", st[1]);
        call.resolve(ret);
      } catch (Exception e) { call.reject("write failed: " + e.getMessage()); }
    });
  }

  @PluginMethod
  public void move(PluginCall call) {
    final String id = call.getString("id", ""), from = call.getString("from", ""), to = call.getString("to", "");
    getBridge().execute(() -> {
      try {
        SafFs f = fs(id);
        ContentResolver cr = getContext().getContentResolver();
        Uri tree = f.tree();
        String srcId = f.resolve(from, false);
        if (srcId == null) { call.reject("not found: " + from); return; }
        Uri src = f.docUri(srcId);
        if (SafFs.dirName(from).equals(SafFs.dirName(to))) {
          DocumentsContract.renameDocument(cr, src, SafFs.baseName(to));
        } else {
          String srcDir = f.resolve(SafFs.dirName(from), false), dstDir = f.resolve(SafFs.dirName(to), true);
          if (dstDir == null) { call.reject("cannot create " + SafFs.dirName(to)); return; }
          DocumentsContract.moveDocument(cr, src, f.docUri(srcDir), f.docUri(dstDir));
          String moved = f.childId(dstDir, SafFs.baseName(from));
          if (moved != null && !SafFs.baseName(from).equals(SafFs.baseName(to))) {
            DocumentsContract.renameDocument(cr, f.docUri(moved), SafFs.baseName(to));
          }
        }
        call.resolve();
      } catch (Exception e) { call.reject("move failed: " + e.getMessage()); }
    });
  }

  @PluginMethod
  public void trash(PluginCall call) {
    final String id = call.getString("id", ""), rel = call.getString("rel", "");
    final Long when = call.getLong("when", 0L);
    getBridge().execute(() -> {
      try {
        // A FAILED TRASH IS A FAILURE — SafFs.trash throws rather than answering {to:null}, because
        // resolving made the sweep agree a tombstone for a file still on disk, which the next sweep
        // then read as a local edit and RE-UPLOADED.
        JSObject ret = new JSObject();
        ret.put("to", fs(id).trash(rel, when == null ? 0L : when));
        call.resolve(ret);
      } catch (Exception e) { call.reject("delete failed: " + e.getMessage()); }
    });
  }

  @PluginMethod
  public void emptyTrash(PluginCall call) {
    final String id = call.getString("id", "");
    // 0 MEANS EVERYTHING, and `getInt(k, 30)` cannot tell an explicit 0 from an absent value on its
    // own — which is the same `|| 30` that made the desktop's Empty trash unable to empty anything
    // newer than a month. Only a genuinely missing value falls back to the safety window.
    final Integer daysArg = call.getInt("days");
    final int days = daysArg == null ? 30 : daysArg;
    getBridge().execute(() -> {
      try {
        SafFs f = fs(id);
        ContentResolver cr = getContext().getContentResolver();
        String trashId = f.resolve(SafFs.TRASH, false);
        int removed = 0;
        if (trashId != null) {
          long cutoff = System.currentTimeMillis() - (long) days * 86400000L;
          Cursor c = cr.query(DocumentsContract.buildChildDocumentsUriUsingTree(f.tree(), trashId),
                              SafFs.COLS, null, null, null);
          if (c != null) {
            while (c.moveToNext()) {
              String docId = c.getString(0), name = c.getString(1);
              // days == 0 is "everything", and the name is not consulted for it: a future-dated
              // folder (a device whose clock was wrong) and one whose name is not a date at all both
              // survive a date comparison for ever, in the one place a user goes to reclaim space.
              if (days > 0) {
                long when = Excludes.dayMillis(name);
                if (when <= 0 || when >= cutoff) continue;
              }
              if (f.deleteDoc(docId)) removed++;
            }
            c.close();
          }
        }
        JSObject ret = new JSObject();
        ret.put("removed", removed);
        call.resolve(ret);
      } catch (Exception e) { call.reject("empty trash failed: " + e.getMessage()); }
    });
  }

  /** SAF has no tree change notification worth having, and polling one is the battery bug the sync
   *  policy exists to avoid. The answer is honest rather than a no-op that pretends. */
  @PluginMethod
  public void watch(PluginCall call) {
    JSObject o = new JSObject();
    o.put("watching", false);
    call.resolve(o);
  }

  @PluginMethod
  public void unwatch(PluginCall call) { call.resolve(); }

  /* The background CHANGE CHECK — see SyncCheckWorker for why it can only detect and not upload.
   * Scheduling is idempotent (KEEP), so calling this on every app start does not reset the period
   * and starve a job that has been waiting for a charger. */
  @PluginMethod
  public void backgroundCheck(PluginCall call) {
    boolean enabled = Boolean.TRUE.equals(call.getBoolean("enabled", false));
    Integer mins = call.getInt("minutes", 180);
    SyncCheckWorker.schedule(getContext(), enabled, mins == null ? 180 : mins);
    JSObject o = new JSObject();
    o.put("enabled", enabled);
    call.resolve(o);
  }

  /** Called after a real sweep so the next check compares against what was actually synced, rather
   *  than against the last thing we happened to mention to the user. */
  @PluginMethod
  public void markSynced(PluginCall call) {
    try { SyncCheckWorker.markSynced(getContext()); } catch (Exception ignored) {}
    call.resolve();
  }

  /** What the battery policy reads (foldersync.js shouldSync). */
  @PluginMethod
  public void power(PluginCall call) {
    JSObject o = new JSObject();
    Context ctx = getContext();
    try {
      BatteryManager bm = (BatteryManager) ctx.getSystemService(Context.BATTERY_SERVICE);
      o.put("charging", bm != null && bm.isCharging());
      if (bm != null) o.put("battery", bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY));
    } catch (Exception e) { o.put("charging", false); }
    try {
      ConnectivityManager cm = (ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE);
      NetworkCapabilities nc = cm == null ? null : cm.getNetworkCapabilities(cm.getActiveNetwork());
      o.put("online", nc != null);
      // NOT_METERED is the capability that actually reflects the user's own "this is metered" flag
      // on a hotspot, which a Wi-Fi-vs-cellular check gets wrong every time.
      o.put("metered", nc != null && !nc.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED));
    } catch (Exception e) { o.put("online", true); o.put("metered", false); }
    call.resolve(o);
  }

  // ---- helpers ----------------------------------------------------------------------------------

  private static List<String> strings(JSArray a) {
    List<String> out = new ArrayList<>();
    if (a == null) return out;
    try { for (Object o : a.toList()) if (o != null) out.add(String.valueOf(o)); } catch (Exception ignored) {}
    return out;
  }

  private String prettyName(Uri tree) {
    String d = DocumentsContract.getTreeDocumentId(tree);
    int i = d == null ? -1 : d.lastIndexOf(':');
    String tail = i >= 0 ? d.substring(i + 1) : d;
    return (tail == null || tail.isEmpty()) ? tree.getLastPathSegment() : tail;
  }
}
