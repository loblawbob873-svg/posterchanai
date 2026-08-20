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
import place.poster.app.home.LauncherState;

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

    /** What a tick would do, worked out without doing any of it. */
    private static final class Plan {
        final List<SyncStore.Folder> due = new ArrayList<SyncStore.Folder>();
        final List<Boolean> deep = new ArrayList<Boolean>();
    }

    /**
     * Every gate a sweep has to pass, and nothing started.
     *
     * SEPARATE FROM {@link #tick} SO THE CALLER CAN ASK BEFORE IT SPENDS ANYTHING. The tick now runs
     * inside a foreground service, and starting one to discover that no folder was due would put an
     * item in somebody's shade for a fraction of a second, every sixteen minutes, for ever. Reading
     * the answer costs a SharedPreferences read and some arithmetic.
     */
    static Plan plan(Context ctx) {
        Context app = ctx.getApplicationContext();
        SyncStore store = new SyncStore(app);
        if (!store.nativeEnabled()) { lastWhy = "native sweeps are off"; return null; }
        if (store.wrappedDriveKey().isEmpty()) { lastWhy = "no drive key handed over yet"; return null; }
        if (!SignerKey.have(app)) {
            /* Amber / a bunker: the account key is not on this device, so nothing here can sign an
             * upload. This is not a failure, it is the shape of that account.
             *
             * FOR A LOCAL KEY IT USED TO BE A BUG, and the most expensive one in this feature: the
             * only things that ever put a key in Keystore were the "Sign for other apps on this
             * phone" switch and pairing a laptop over NIP-46 — two unrelated features in two other
             * parts of settings. So an ordinary account that had touched neither answered "not on
             * this device" about a key that was sitting in the WebView the whole time, and the
             * native sweep never ran once. sync.js `_pushNativeConfig` arms it now. */
            lastWhy = "the account key is not on this device";
            return null;
        }

        /* THE APP IS OPEN — LET THE PAGE DO IT. See FolderSyncPlugin.appInForeground.
         *
         * Two engines racing for the same folder is not merely wasteful: the loser is the one the
         * user is looking at, and it has nothing to show but "already syncing". This is also the
         * honest division, because the whole reason a native sweep exists is that a HIDDEN page's
         * JavaScript is throttled — a visible one is not, and it can do strictly more (settle
         * conflicts, ask about a mass delete, draw a progress bar). */
        if (FolderSyncPlugin.appInForeground()) {
            lastWhy = "the app is open — the page sweeps while you can see it";
            return null;
        }

        /* AND OUR OWN HOME SCREEN IS NOT "the app is closed" EITHER.
         *
         * As the launcher, MainActivity pausing means somebody pressed HOME — the resting state of
         * the phone, dozens of times an hour, every one of them with the screen on and a finger on
         * the glass. Starting a sweep there takes the folder's claim moments before they open
         * PosterChan, and a page refused its claim can only say "syncing in the background". The
         * comment above calls that a hang the user caused by opening the app; as a launcher it stops
         * being a coincidence and becomes the ordinary way the app is opened.
         *
         * The window is seconds long in real use, so the cost of standing down is one missed alarm,
         * and LauncherState bounds it so a phone parked awake on its home screen is late rather than
         * never. Screen OFF at the home screen does NOT defer — that is the best time to sync. */
        if (LauncherState.deferSweep(interactive(app), System.currentTimeMillis())) {
            lastWhy = "you are on the home screen — syncing waits until the phone is idle";
            return null;
        }

        Plan p = new Plan();
        Map<String, Object> state = deviceState(app);
        for (SyncStore.Folder f : store.folders()) {
            /* A FOLDER SOMEBODY ELSE IS ALREADY SWEEPING IS NOT DUE. Without this the app being OPEN
             * — the case where the page claims every folder before the alarm lands — still answered
             * "eligible", so a foreground service started, `NativeSweep.run` was refused its claim on
             * every folder, and the whole thing amounted to a notification appearing and vanishing.
             * Asked, never taken: `plan()` decides and does not sweep, so a claim taken here would
             * have to be given back on every path that decides not to. */
            if (NativeSweep.claimed(f.key)) continue;
            Map<String, Object> s = new LinkedHashMap<String, Object>(state);
            s.put("lastSyncAt", store.lastSyncAt(f.key));
            s.put("lastFullScanAt", store.lastFullScanAt(f.key));
            Map<String, Object> verdict = SyncDiff.shouldSync(s, prefsOf(f));
            // `metadata` means "note the changes, move bytes later" — there is no cheaper thing for
            // this path to do than what a sweep already does, so only the two real modes run.
            String mode = Json.str(verdict.get("mode"), "none");
            if ("incremental".equals(mode) || "full".equals(mode)) {
                p.due.add(f);
                /* `full` IS THE ONE THAT REHASHES, and it has to be carried through or the mode is a
                 * label with nothing behind it — the folder would be marked as fully checked by a
                 * sweep that only compared sizes and timestamps, and would then not be checked again
                 * for a day. `lastFullScanAt` was also never recorded, so every sweep on a charger
                 * answered `full`. */
                p.deep.add("full".equals(mode));
            }
        }
        if (p.due.isEmpty()) { lastWhy = "no folder is due"; return null; }
        return p;
    }

    /** @return true when a sweep would run right now — asked before a foreground service is started. */
    /** Whether the display is on. Its own method so LauncherState can stay off-device testable. */
    private static boolean interactive(Context ctx) {
        try {
            PowerManager pm = (PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
            return pm != null && pm.isInteractive();
        } catch (Throwable t) {
            // Unknowable is treated as NOT interactive: the deferral is an optimisation, and failing
            // it closed would mean a phone whose power state cannot be read never syncs.
            return false;
        }
    }

    public static boolean eligible(Context ctx) {
        if (running) { lastWhy = "a native sweep is already running"; return false; }
        return plan(ctx) != null;
    }

    public static boolean tick(Context ctx, String why) { return tick(ctx, why, null); }

    /**
     * @param done run on the sweep thread when the sweep finishes, however it finishes. This is how
     *             {@link SyncService} knows to stand down — polling `busy()` would be a timer inside
     *             the very state (screen off, process about to be frozen) timers cannot be trusted in.
     * @return true when a native sweep has been started, so the caller must NOT also ask the WebView
     *         to sweep. False means "not my job" — the tick goes to JavaScript exactly as before.
     */
    public static boolean tick(Context ctx, String why, final Runnable done) {
        final Context app = ctx.getApplicationContext();
        if (running) { lastWhy = "a native sweep is already running"; return false; }
        final Plan p = plan(app);
        if (p == null) return false;

        running = true;
        lastWhy = "sweeping " + p.due.size() + " folder" + (p.due.size() == 1 ? "" : "s")
                + (why == null || why.isEmpty() ? "" : " (" + why + ")");
        Thread t = new Thread(new Runnable() {
            public void run() {
                try { sweepAll(app, p.due, p.deep); }
                finally { if (done != null) { try { done.run(); } catch (Throwable ignored) { } } }
            }
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
            /* "I COULD NOT READ THE NETWORK" IS NOT "THERE IS NO NETWORK", AND CONFLATING THEM IS
             * WHY THE BACKGROUND SWEEP NEVER RAN.
             *
             * This used to be `s.put("online", nc != null)` — a null read asserted OFFLINE, and
             * `shouldSync` answers `mode: none, why: offline` to that, so `plan()` found no folder
             * due and the whole chain above it declined. The alarm fired, the receiver ran, and
             * nothing swept.
             *
             * `getNetworkCapabilities(getActiveNetwork())` returning null is not rare in the state
             * this code runs in: it is exactly what a dozing device can answer, and the alarm exists
             * to fire while the device is dozing. So the one moment the sweep was designed for was
             * the one moment it read itself as offline — working whenever the app was open (network
             * live, capabilities readable) and never with the screen off. Reported, precisely, as
             * "background sync stops shortly after you turn the screen off".
             *
             * So an unreadable network is left UNSET, and `shouldSync` skips a check it has no
             * answer for (`s.get("online") != null` guards it). The same rule `suppressed()` already
             * states for metered — an unreliable read must not stop background sync outright — now
             * applied to the fact that was silently stopping it. Being wrong costs one sweep whose
             * requests fail against a bounded timeout and are retried by the next tick. */
            if (nc != null) {
                s.put("online", true);
                // NOT_METERED reflects the user's own "this is metered" flag on a hotspot, which a
                // Wi-Fi-vs-cellular check gets wrong every time.
                s.put("metered", !nc.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED));
            } else if (cm != null && cm.getActiveNetwork() == null) {
                // A readable manager reporting NO active network at all IS a real answer: flight
                // mode, or genuinely nothing up. Only an unreadable one is left unknown.
                s.put("online", false);
            }
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
