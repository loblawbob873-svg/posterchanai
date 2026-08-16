package place.poster.app.sync;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.os.BatteryManager;
import android.os.PowerManager;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import place.poster.app.signer.SignerKey;

/**
 * The thing the alarm actually calls: decide whether this phone can sweep on its own, and if so, do
 * it — off the main thread, holding the processor, without the WebView.
 *
 * IT IS ALLOWED TO ANSWER "NO", AND SAYING SO IS THE POINT. Two accounts cannot be swept natively:
 * one signed in through Amber or a bunker (the key is not on this device, and every network step of
 * a sweep is signed), and one whose page has not handed over its configuration yet. Both fall back
 * to the tick that asks the WebView, which is what shipped before this and still works whenever the
 * page happens to be running. Nothing here is a replacement for that path; it is the case that path
 * cannot cover.
 *
 * THE WAKE LOCK IS TAKEN HERE, not by the sweep. A foreground service keeps the PROCESS resident and
 * does nothing about the PROCESSOR — measured on a real phone as 23 downloads in the minute before
 * the screen went off and 0 in the minute after — and this is the one place that knows a sweep is
 * genuinely about to run. It is TIMED and RENEWED on a clock: a timed lock is not extended by being
 * held, and renewing on progress is what left a long download without one (`getParts` had no
 * progress callback). A crash cannot leak it past the bound, and the finally releases it.
 */
public final class NativeRunner {

    private NativeRunner() { }

    private static final long WAKE_MAX_MS = 10 * 60 * 1000L;
    private static final long RENEW_MS = 60 * 1000L;

    private static volatile boolean running = false;
    private static volatile String lastWhy = "";

    public static boolean busy() { return running; }

    public static String why() { return lastWhy; }

    /**
     * @return true when a native sweep has been started, so the caller must NOT also ask the WebView
     *         to sweep. False means "not my job" — the tick goes to JavaScript exactly as before.
     */
    public static boolean tick(Context ctx, String why) {
        Context app = ctx.getApplicationContext();
        SyncStore store = new SyncStore(app);
        if (!store.nativeEnabled()) { lastWhy = "native sweeps are off"; return false; }
        if (store.wrappedDriveKey().isEmpty()) { lastWhy = "no drive key handed over yet"; return false; }
        if (!SignerKey.have(app)) {
            // Amber / a bunker: the account key is not on this device, so nothing here can sign an
            // upload. This is not a failure, it is the shape of that account.
            lastWhy = "the account key is not on this device";
            return false;
        }
        if (running) { lastWhy = "a native sweep is already running"; return true; }

        final List<SyncStore.Folder> due = new ArrayList<SyncStore.Folder>();
        final List<Boolean> deep = new ArrayList<Boolean>();
        Map<String, Object> state = deviceState(app);
        for (SyncStore.Folder f : store.folders()) {
            Map<String, Object> s = new LinkedHashMap<String, Object>(state);
            s.put("lastSyncAt", store.lastSyncAt(f.key));
            s.put("lastFullScanAt", store.lastFullScanAt(f.key));
            Map<String, Object> verdict = SyncDiff.shouldSync(s, prefsOf(f));
            // `metadata` means "note the changes, move bytes later" — there is no cheaper thing for
            // this path to do than what a sweep already does, so only the two real modes run.
            String mode = Json.str(verdict.get("mode"), "none");
            if ("incremental".equals(mode) || "full".equals(mode)) {
                due.add(f);
                /* `full` IS THE ONE THAT REHASHES, and it has to be carried through or the mode is a
                 * label with nothing behind it — the folder would be marked as fully checked by a
                 * sweep that only compared sizes and timestamps, and would then not be checked again
                 * for a day. `lastFullScanAt` was also never recorded, so every sweep on a charger
                 * answered `full`. */
                deep.add("full".equals(mode));
            }
        }
        if (due.isEmpty()) { lastWhy = "no folder is due"; return false; }

        running = true;
        lastWhy = "sweeping " + due.size() + " folder" + (due.size() == 1 ? "" : "s");
        final Context fctx = app;
        Thread t = new Thread(new Runnable() {
            public void run() { sweepAll(fctx, due, deep); }
        }, "pc-native-sync");
        t.setPriority(Thread.MIN_PRIORITY + 2);
        t.start();
        return true;
    }

    private static void sweepAll(Context ctx, List<SyncStore.Folder> due, List<Boolean> deep) {
        SyncStore store = new SyncStore(ctx);
        PowerManager.WakeLock wake = null;
        java.util.Timer renew = null;
        byte[] sec = null;
        try {
            PowerManager pm = (PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
            if (pm != null) {
                wake = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "posterchan:nativesync");
                wake.setReferenceCounted(false);
                wake.acquire(WAKE_MAX_MS);
                final PowerManager.WakeLock held = wake;
                renew = new java.util.Timer("pc-native-sync-wake", true);
                renew.schedule(new java.util.TimerTask() {
                    public void run() {
                        // A timed lock is NOT renewed by being held: the OS reclaims it when the
                        // timeout expires, so a sweep longer than the bound loses the processor
                        // part-way and stops mid-file. A clock needs no cooperation from the
                        // transfer, which is what a progress callback did need and did not have.
                        try { held.acquire(WAKE_MAX_MS); } catch (Throwable ignored) { }
                    }
                }, RENEW_MS, RENEW_MS);
            }
            sec = SignerKey.load(ctx);
            if (sec == null) { lastWhy = "the stored key could not be read"; return; }

            List<Object> reports = new ArrayList<Object>();
            for (int i = 0; i < due.size(); i++) {
                SyncStore.Folder f = due.get(i);
                boolean hash = i < deep.size() && Boolean.TRUE.equals(deep.get(i));
                NativeSweep.Report rep = NativeSweep.run(ctx, store, f, sec, hash, null);
                reports.add(rep.toMap());
                /* THE CLOCK ADVANCES WHEN THE SWEEP COMPLETED, and a DEFERRAL is a completion.
                 *
                 * It used to require `deferred == 0` too, which reads as caution and is a battery
                 * leak: a conflict is deferred by this sweep by design and may never be settled
                 * until somebody opens the app, so that folder would be swept again on every single
                 * alarm, for ever, to defer it again. An ERROR is different — nothing was learned —
                 * and still holds the clock back so the next tick retries promptly. */
                if (rep.error.isEmpty()) {
                    store.setLastSyncAt(f.key, rep.at);
                    if (hash) store.setLastFullScanAt(f.key, rep.at);
                }
            }
            Map<String, Object> out = new LinkedHashMap<String, Object>();
            out.put("at", System.currentTimeMillis());
            out.put("folders", reports);
            store.setLastReport(Json.write(out));
        } catch (Throwable t) {
            Map<String, Object> out = new LinkedHashMap<String, Object>();
            out.put("at", System.currentTimeMillis());
            out.put("error", String.valueOf(t.getMessage() == null ? t : t.getMessage()));
            try { store.setLastReport(Json.write(out)); } catch (Throwable ignored) { }
        } finally {
            if (sec != null) java.util.Arrays.fill(sec, (byte) 0);
            if (renew != null) try { renew.cancel(); } catch (Throwable ignored) { }
            if (wake != null) try { if (wake.isHeld()) wake.release(); } catch (Throwable ignored) { }
            running = false;
        }
    }

    /** The same two facts `shouldSync` reads in the browser, from the same APIs. */
    static Map<String, Object> deviceState(Context ctx) {
        Map<String, Object> s = new LinkedHashMap<String, Object>();
        s.put("now", System.currentTimeMillis());
        try {
            BatteryManager bm = (BatteryManager) ctx.getSystemService(Context.BATTERY_SERVICE);
            if (bm != null) {
                s.put("charging", bm.isCharging());
                s.put("battery", (long) bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY));
            }
        } catch (Throwable ignored) { }
        try {
            ConnectivityManager cm = (ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE);
            NetworkCapabilities nc = cm == null ? null : cm.getNetworkCapabilities(cm.getActiveNetwork());
            s.put("online", nc != null);
            // NOT_METERED is the capability that reflects the user's own "this is metered" flag on a
            // hotspot, which a Wi-Fi-vs-cellular check gets wrong every time.
            s.put("metered", nc != null && !nc.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED));
        } catch (Throwable ignored) { }
        return s;
    }

    static Map<String, Object> prefsOf(SyncStore.Folder f) {
        Map<String, Object> p = new LinkedHashMap<String, Object>();
        p.put("enabled", f.enabled);
        p.put("paused", f.paused);
        p.put("onlyWhenCharging", f.onlyWhenCharging);
        p.put("wifiOnly", f.wifiOnly);
        p.put("minBattery", (long) f.minBattery);
        return p;
    }
}
