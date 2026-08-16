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
 * Background sync runs off {@link SyncClock} — folder sync's OWN alarm, armed from configure() below
 * whenever this account syncs anything — landing in {@link SyncTickReceiver} and sweeping inside
 * {@link SyncService}. It used to ride "Stay connected"'s foreground service, which is off by
 * default and is a notifications feature: a phone that had never turned that switch on had no clock
 * at all, so none of the machinery below ever ran. That was the bug.
 * The WorkManager job (SyncCheckWorker) is still only a notifier, for the reason it always was: an
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
   * `delivered` counts ticks handed to a live bridge, `dropped` ticks with no bridge to hand them
   * to, `suppressed` ticks the battery pre-filter skipped. The ALARM's own counters live with the
   * alarm, in {@link SyncClock} — one clock, one place that knows whether it is running.
   *
   * DO NOT READ `armed > fired + 1` AS DOZE EATING ALARMS — that was the first reading written here
   * and it is wrong. Only ONE alarm is ever outstanding (`FLAG_UPDATE_CURRENT` replaces the pending
   * one rather than adding to it), and every configure() from the page arms again: a page load, a
   * sweep, a folder added. So `armed` is a count of schedulings, not of alarms in flight. */
  private static final java.util.concurrent.atomic.AtomicInteger tDelivered = new java.util.concurrent.atomic.AtomicInteger();
  private static final java.util.concurrent.atomic.AtomicInteger tDropped = new java.util.concurrent.atomic.AtomicInteger();
  private static volatile long tLastDelivered = 0;

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
    SyncStore store = new SyncStore(getContext());
    try {
      JSArray folders = call.getArray("folders");
      store.configure(
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
    /* AND ARM THE CLOCK, which is the whole of "background sync does not work on this phone".
     *
     * It used to be armed by StayAwakeService — the "Stay connected" switch, off by default, in the
     * notifications settings, described as a fallback for DMs and calls. A phone that had never
     * touched it had no clock, so nothing ever asked for a background sweep and folder sync stopped
     * with the screen. The feature asks for its own clock now.
     *
     * ON EVERY CONFIGURE, not once: an alarm does not survive a reboot, a force-stop clears it, and
     * FLAG_UPDATE_CURRENT means re-arming replaces rather than multiplies. The page calls this at
     * startup and after every sweep, so opening the app is what puts the clock back.
     *
     * CANCELLED when nothing is left to sync — including when this account signs out — because an
     * alarm that wakes the phone every sixteen minutes to decide there is no work is exactly the
     * battery complaint that gets a sync feature turned off. */
    SyncClock.followStore(getContext());
    call.resolve();
  }

  /** Sign-out, or the switch turned off: the wrapped key must not outlive the session that set it. */
  @PluginMethod
  public void forgetNative(PluginCall call) {
    new SyncStore(getContext()).forget();
    // …and stop waking the phone for an account that is no longer here.
    SyncClock.cancel(getContext());
    call.resolve();
  }

  /** What the last unattended sweep actually did — the only surface any of it can be read from. */
  @PluginMethod
  public void nativeReport(PluginCall call) {
    SyncStore store = new SyncStore(getContext());
    JSObject o = new JSObject();
    o.put("enabled", store.nativeEnabled());
    // WHY it is off, when it is. "native sweep: off" on its own sent this investigation down two
    // wrong paths; the store knows exactly which of the four facts is missing, so it says.
    o.put("why_off", store.whyDisabled());
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

  /* IS THE APP ON SCREEN RIGHT NOW.
   *
   * THE NATIVE SWEEP MUST NOT COMPETE WITH THE PAGE, and until the clock started firing it never
   * had the chance to. Making background sync actually run turned a dormant conflict into the
   * reported one: the alarm claims "Pictures", the person looking at the app presses Sync now, the
   * page's claim is refused, and the card sits on "syncing in the background — it will finish on its
   * own" with no progress of its own to show. From the user's side that is a hang, and it is a hang
   * they caused by opening the app.
   *
   * The division is by VISIBILITY, which is also the honest one: the native sweep exists because a
   * hidden page's JavaScript is throttled. A page that is on screen is not throttled — it is the
   * better engine, it can settle conflicts the background sweep defers, and it can show progress. So
   * while the app is up, the page owns the folders and the alarm stands down; the moment it is
   * backgrounded (including the screen going off, which is what `onPause` means here) the native
   * sweep takes over.
   */
  private static volatile boolean foreground = false;

  public static boolean appInForeground() { return foreground; }

  /** Visible ONLY so the stand-down can be run in a test — there is no device in this loop, and the
   *  alternative is asserting on the text of the `if` that implements it. */
  static void setForegroundForTest(boolean on) { foreground = on; }

  @Override
  public void handleOnResume() { foreground = true; super.handleOnResume(); }

  @Override
  public void handleOnPause() { foreground = false; super.handleOnPause(); }

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
    o.put("armed", SyncClock.armedCount());
    o.put("fired", SyncClock.firedCount());
    o.put("delivered", tDelivered.get());
    o.put("dropped", tDropped.get());
    o.put("suppressed", tSuppressed.get());
    o.put("needCharging", pNeedCharging);
    o.put("needUnmetered", pNeedUnmetered);
    /* THE CLOCK, WHICH IS NOW FOLDER SYNC'S OWN. Reported as two WALL-CLOCK TIMES that survive a
     * process death, not as a flag: the panel can only be opened from a running page, and a running
     * page has already armed, so any "is it armed" boolean reads true to everyone who can ask it.
     * "armed 4 min ago, last fired never" is a clock that is not running and says so on sight. */
    o.put("lastArmedAt", SyncClock.lastArmedAt(getContext()));
    o.put("lastFiredAt", SyncClock.lastFiredAt(getContext()));
    o.put("lastDeliveredAt", tLastDelivered);
    o.put("clockArmed", SyncClock.armedThisProcess());
    o.put("clockPeriodMin", (int) (SyncClock.PERIOD_MS / 60000L));
    /* WHETHER THE ALARM IS EXACT, which sounds like a detail about punctuality and is the thing that
     * decides whether a background sweep may hold the process at all: Android 12+ only lets an
     * EXACT alarm start a foreground service. On 13+ that is a permission the user grants, so this
     * being false is normal — and it is why `job` exists beside `foreground` below. */
    o.put("clockExact", SyncClock.isExact(getContext()));
    /* WHICH ROUTE THE SWEEP ACTUALLY GOT. `foreground` = a foreground service (best: no time limit).
     * `refused` = Android would not allow one. `job` = the expedited-job fallback, which also keeps
     * the process out of the freezer but is capped at about ten minutes. All three are counted
     * because from every other vantage point they look identical — silence. */
    o.put("foreground", SyncClock.foregroundCount());
    o.put("foregroundRefused", SyncClock.refusedCount());
    o.put("job", SyncClock.jobCount());
    o.put("sweepServiceUp", SyncService.running);
    // Still reported, because it is no longer REQUIRED and somebody will ask whether it is.
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

    final int offset = call.getInt("offset", 0) == null ? 0 : call.getInt("offset", 0);
    final int limit = call.getInt("limit", 0) == null ? 0 : call.getInt("limit", 0);

    // Off the WebView thread: a Pictures folder is minutes of provider queries, and blocking here
    // freezes the UI the user is watching the progress in.
    getBridge().execute(() -> {
      try {
        final String key = id + "|" + hash + "|" + maxBytes + "|" + excludes;
        SafFs.Scan sc;
        java.util.List<String> keys;
        synchronized (SCAN_LOCK) {
          if (offset == 0 || scanCache == null || !key.equals(scanCacheKey)) {
            sc = fs(id).scan(hash, maxBytes, excludes);
            scanCache = sc;
            scanCacheKey = key;
            scanKeys = new ArrayList<String>(sc.files.keySet());
          } else {
            sc = scanCache;
          }
          keys = scanKeys;
        }

        final int total = keys.size();
        final int end = limit <= 0 ? total : Math.min(total, offset + limit);
        JSObject files = new JSObject();
        for (int i = offset; i < end; i++) {
          String k = keys.get(i);
          files.put(k, mapToJs(sc.files.get(k)));
        }
        JSArray skipped = new JSArray();
        // Only with the first page: it is small, and eleven copies of it is eleven copies.
        if (offset == 0) for (java.util.Map<String, Object> s : sc.skipped) skipped.put(mapToJs(s));

        JSObject ret = new JSObject();
        ret.put("files", files);
        ret.put("skipped", skipped);
        ret.put("total", total);
        ret.put("done", end >= total);
        // Let the map go the moment the last page is out. Holding a 15,000-entry snapshot for the
        // life of the process would trade one memory spike for a permanent one.
        if (end >= total) {
          synchronized (SCAN_LOCK) {
            if (key.equals(scanCacheKey)) { scanCache = null; scanKeys = null; scanCacheKey = ""; }
          }
        }
        call.resolve(ret);
      } catch (Exception e) {
        call.reject("scan failed: " + e.getMessage());
      }
    });
  }

  /* THE FOLDER LISTING IS HANDED OVER IN PAGES, AND THAT IS WHAT STOPPED THE APP DYING.
   *
   * This used to answer with every file in one reply. For a real Pictures folder — measured at about
   * 15,000 files — that object exists FOUR TIMES AT ONCE at the moment it crosses: the Java map, the
   * org.json copy built here, the multi-megabyte JSON STRING Capacitor serialises it into to cross
   * the bridge, and the parsed object on the other side. A WebView has far less headroom than the
   * desktop this engine was written on, and the result is the renderer being killed the instant a
   * sweep of that folder starts — reported exactly that way: "as soon as pictures starts syncing, it
   * closes", with the app still sitting in the recents list, because the PROCESS never died. Only the
   * renderer did, which is why nothing was ever thrown, logged, or catchable.
   *
   * The same reasoning already produced `readPart`/`writePart` for file CONTENT; the directory
   * listing was the one whole-folder object left.
   *
   * The walk itself still happens once — it is minutes of provider queries and must not be repeated
   * per page — so the result is cached here and served in slices. `limit <= 0` keeps the old
   * behaviour intact, which is what an older client that does not page still asks for. */
  private static final Object SCAN_LOCK = new Object();
  private static SafFs.Scan scanCache;
  private static String scanCacheKey = "";
  private static java.util.List<String> scanKeys;

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

  /**
   * A FILE'S CONTENT IDENTITY, WITHOUT THE FILE EVER CROSSING THE BRIDGE.
   *
   * Settling a conflict means asking "are these the same bytes", and the only way the engine had was
   * `read()` above: the whole file into the plugin, base64 to cross (four characters per three
   * bytes, held as UTF-16), then a hash pass over it in the renderer. Tens of megabytes per photo,
   * per conflict. Measured on a real folder: 1,927 conflicts, dead on the first one.
   *
   * The scan has always hashed the streamed way. This is that, for one path.
   */
  @PluginMethod
  public void hashFile(PluginCall call) {
    final String id = call.getString("id", ""), rel = call.getString("rel", "");
    getBridge().execute(() -> {
      try {
        JSObject ret = new JSObject();
        ret.put("sha", fs(id).sha256Of(rel));
        call.resolve(ret);
      } catch (Exception e) { call.reject("hash failed: " + e.getMessage()); }
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
