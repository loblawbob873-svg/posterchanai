package place.poster.app.sync;

import android.content.Context;
import android.content.SharedPreferences;

import java.io.File;
import java.io.FileOutputStream;
import java.io.RandomAccessFile;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * What the phone needs to sweep on its own: where the servers are, which folders are paired, and
 * what this device last agreed each one contained.
 *
 * THE CLIENT HANDS IT OVER; NOTHING HERE IS INVENTED. Every value below is one the WebView already
 * knows and the phone cannot work out for itself — the instance URL, the media server, the pair
 * keys, the exclusion lists, the per-folder switches. `configure()` is called on every foreground
 * sweep and on startup, so a background sweep is always working from the settings the user last saw
 * on screen rather than from a copy that can silently go stale.
 *
 * THE DRIVE KEY IS STORED WRAPPED, and that is the whole reason a native sweep is acceptable at all.
 * What lands on disk is the NIP-44 self-wrapped form the drive index already holds — unreadable
 * without the account's Nostr secret, which the native signer holds already. So this adds no new
 * secret at rest: an attacker with the file has what the server has, which is nothing.
 *
 * `base` IS A FILE, NOT A PREFERENCE. It is the per-path agreement, and on a real 15,790-file folder
 * it is megabytes — SharedPreferences holds every value in memory and rewrites the whole XML on each
 * commit, which is exactly the trap the browser fell into with localStorage (a swallowed quota error
 * and an infinite resync). One JSON file per pair key, written whole, read whole.
 */
public final class SyncStore {

    private static final String PREFS = "pc_sync_native";
    private static final String K_API = "apiBase";
    private static final String K_MEDIA = "mediaBase";
    private static final String K_MK = "mkWrapped";
    private static final String K_DEVICE = "device";
    private static final String K_FOLDERS = "folders";
    private static final String K_ENABLED = "nativeEnabled";
    private static final String K_LAST = "lastReport";

    private final Context ctx;

    public SyncStore(Context ctx) { this.ctx = ctx.getApplicationContext(); }

    private SharedPreferences prefs() {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    // ------------------------------------------------------------------------------- config

    /** One paired folder as the client sees it. */
    public static final class Folder {
        public String key = "";          // the PAIR key — the name, the same on every device
        public String id = "";           // this device's SAF tree URI
        public final List<String> excludes = new ArrayList<String>();
        public boolean enabled = true;
        public boolean paused = false;
        public boolean onlyWhenCharging = false;
        public boolean wifiOnly = true;
        public int minBattery = 20;
    }

    /**
     * DERIVED, NEVER STORED — and this is the bug that survived the first fix.
     *
     * The page used to compute this and push it as a boolean: `mk && api && media && folders`. But
     * `mk` is `FilesIdx._mkWrapped`, which is populated from localStorage when the drive index
     * loads, and `configure()` is called at STARTUP — before that has necessarily happened. So an
     * ordinary app launch pushed `enabled:false` and OVERWROTE a true value, and the next alarm
     * found the feature switched off. The clock fired, the receiver ran, `plan()` read this flag
     * first and answered "native sweeps are off": nothing swept, and nothing anywhere said why.
     * Opening Folder Sync and pressing Sync now put it back — which is exactly the shape of "it only
     * syncs when I open the app", the report this whole change exists to end.
     *
     * The stored key already had this rule ("AN ABSENT KEY DOES NOT ERASE THE ONE WE HAVE", below);
     * the flag derived from it did not, which made that protection useless. So there is no flag any
     * more: the answer is computed from what is actually on disk, where a value that was never
     * pushed simply leaves the previous one in place. A transient empty push can no longer turn
     * background sync off.
     *
     * `K_ENABLED` survives as an explicit OFF only — set false by {@link #forget()} on sign-out.
     */
    public boolean nativeEnabled() {
        if (!prefs().getBoolean(K_ENABLED, true)) return false;      // signed out: hard off
        return !wrappedDriveKey().isEmpty() && !apiBase().isEmpty()
                && !mediaBase().isEmpty() && !folders().isEmpty();
    }

    /** What {@link #nativeEnabled} is missing, for the panel. Empty string when it is on. */
    public String whyDisabled() {
        if (!prefs().getBoolean(K_ENABLED, true)) return "signed out on this device";
        if (wrappedDriveKey().isEmpty()) return "no drive key handed over yet";
        if (apiBase().isEmpty()) return "no server address";
        if (mediaBase().isEmpty()) return "no media server";
        if (folders().isEmpty()) return "no folders paired on this device";
        return "";
    }

    public String apiBase() { return prefs().getString(K_API, ""); }

    public String mediaBase() { return prefs().getString(K_MEDIA, ""); }

    public String wrappedDriveKey() { return prefs().getString(K_MK, ""); }

    /** Replace a cached key only with the value just read from the account's authoritative index. */
    public void setWrappedDriveKey(String wrappedKey) {
        if (wrappedKey == null || wrappedKey.isEmpty()) return;
        prefs().edit().putString(K_MK, wrappedKey).commit();
    }

    public String deviceName() { return prefs().getString(K_DEVICE, "this phone"); }

    public List<Folder> folders() {
        List<Folder> out = new ArrayList<Folder>();
        String raw = prefs().getString(K_FOLDERS, "[]");
        List<Object> list;
        try { list = Json.arr(Json.parse(raw)); } catch (RuntimeException e) { return out; }
        for (Object o : list) {
            Map<String, Object> m = Json.obj(o);
            Folder f = new Folder();
            f.key = Json.str(m.get("key"), "");
            f.id = Json.str(m.get("id"), "");
            f.enabled = Json.bool(m.get("enabled"), true);
            f.paused = Json.bool(m.get("paused"), false);
            f.onlyWhenCharging = Json.bool(m.get("onlyWhenCharging"), false);
            f.wifiOnly = Json.bool(m.get("wifiOnly"), true);
            f.minBattery = (int) Json.num(m.get("minBattery"), 20);
            for (Object e : Json.arr(m.get("excludes"))) f.excludes.add(String.valueOf(e));
            if (!f.key.isEmpty() && !f.id.isEmpty()) out.add(f);
        }
        return out;
    }

    /** Store what the page just told us. `folders` is the JSON array exactly as the client sent it. */
    public void configure(boolean enabled, String apiBase, String mediaBase, String wrappedKey,
                          String device, String foldersJson) {
        SharedPreferences.Editor e = prefs().edit();
        /* `enabled` FROM THE PAGE IS NOW ONLY ABLE TO TURN THIS BACK ON, never off — see
         * nativeEnabled(). A false here means "I could not see a drive key at this instant", which
         * on a cold start is the normal state and must not disable the feature. Sign-out goes
         * through forget(), which is the one path that means it. */
        if (enabled) e.putBoolean(K_ENABLED, true);
        /* AN EMPTY SERVER DOES NOT ERASE THE STORED ONE, for the same reason the key does not: these
         * come from `PC.serverOrigin()`/`PC.mediaServer()`, which answer '' before the client has
         * resolved its instance, and a startup push would otherwise blank what a working sweep needs. */
        if (apiBase != null && !apiBase.isEmpty()) e.putString(K_API, apiBase);
        if (mediaBase != null && !mediaBase.isEmpty()) e.putString(K_MEDIA, mediaBase);
        /* AN ABSENT KEY DOES NOT ERASE THE ONE WE HAVE. The client only has a drive key once the
         * signer has answered, and a configure() that arrives before that (page load, an account
         * still resolving) would otherwise wipe the very thing that lets the phone sweep alone —
         * turning a working background sync off, silently, until somebody opened the app again. */
        if (wrappedKey != null && !wrappedKey.isEmpty()) e.putString(K_MK, wrappedKey);
        if (device != null && !device.isEmpty()) e.putString(K_DEVICE, device);
        if (foldersJson != null) e.putString(K_FOLDERS, foldersJson);
        e.apply();
    }

    /** Sign-out, or a switch turned off: the wrapped key must not outlive the session that set it. */
    public void forget() {
        prefs().edit().remove(K_MK).putBoolean(K_ENABLED, false).apply();
    }

    // -------------------------------------------------------------------------- last report

    /** What the last native sweep did, for Folder Sync → background details to show. */
    public void setLastReport(String json) { prefs().edit().putString(K_LAST, json).apply(); }

    public String lastReport() { return prefs().getString(K_LAST, ""); }

    /* WHEN THIS FOLDER LAST SETTLED, which is what `shouldSync`'s minimum interval reads.
     *
     * Deliberately the NATIVE clock and not the page's: the two paths sweep the same folder and the
     * page's own record lives in the browser's storage, so sharing one would need a round trip
     * through the very WebView this path exists to work without. The cost of two clocks is at worst
     * one extra sweep after the app has been open, which finds nothing and stops. */
    public long lastSyncAt(String key) {
        return prefs().getLong("lastSync:" + key, 0L);
    }

    public void setLastSyncAt(String key, long when) {
        prefs().edit().putLong("lastSync:" + key, when).apply();
    }

    /** When this folder was last REHASHED, which is what makes `full` mean something on a charger. */
    public long lastFullScanAt(String key) {
        return prefs().getLong("lastFull:" + key, 0L);
    }

    public void setLastFullScanAt(String key, long when) {
        prefs().edit().putLong("lastFull:" + key, when).apply();
    }

    // ---------------------------------------------------------------------- the agreement

    private File baseFile(String key) {
        File dir = new File(ctx.getFilesDir(), "syncbase");
        if (!dir.exists()) dir.mkdirs();
        // Hashed, because a pair key is a user-typed name and can hold anything a filename cannot.
        return new File(dir, SyncCrypto.sha256hex(SyncCrypto.utf8(key)) + ".json");
    }

    /** The pair's cached record set — {era, cursor, entries:{path: entry}} — file-backed like the
     *  journal. Losing it costs one full re-read, never data. */
    public Map<String, Object> stateCache(String key) {
        File f = new File(baseFile(key).getPath() + ".state");
        if (!f.exists()) return null;
        RandomAccessFile r = null;
        try {
            r = new RandomAccessFile(f, "r");
            byte[] buf = new byte[(int) r.length()];
            r.readFully(buf);
            return Json.obj(Json.parse(SyncCrypto.fromUtf8(buf)));
        } catch (Exception ignored) {
            return null;
        } finally { try { if (r != null) r.close(); } catch (Exception ignored) { } }
    }

    public void saveStateCache(String key, Map<String, Object> cache) {
        File f = new File(baseFile(key).getPath() + ".state");
        File tmp = new File(f.getPath() + ".tmp");
        try {
            FileOutputStream out = new FileOutputStream(tmp);
            try { out.write(SyncCrypto.utf8(Json.write(cache))); } finally { out.close(); }
            if (!tmp.renameTo(f)) { f.delete(); tmp.renameTo(f); }
        } catch (Exception ignored) { /* a lost cache is one full re-read */ }
    }

    public Map<String, Map<String, Object>> base(String key) {
        Map<String, Map<String, Object>> out = new LinkedHashMap<String, Map<String, Object>>();
        File f = baseFile(key);
        if (!f.exists()) return out;
        RandomAccessFile r = null;
        try {
            r = new RandomAccessFile(f, "r");
            byte[] buf = new byte[(int) r.length()];
            r.readFully(buf);
            for (Map.Entry<String, Object> e : Json.obj(Json.parse(SyncCrypto.fromUtf8(buf))).entrySet()) {
                out.put(e.getKey(), Json.obj(e.getValue()));
            }
        } catch (Exception ignored) {
            /* A CORRUPT AGREEMENT IS RECOVERABLE AND AN EMPTY ONE IS NOT DANGEROUS HERE: it costs a
             * full compare, never data — the engine's empty-base rule settles identical files by
             * content rather than conflicting them. What must never happen is treating an unreadable
             * MANIFEST this way, which is a different file and a 503. */
        } finally { try { if (r != null) r.close(); } catch (Exception ignored) { } }
        return out;
    }

    /**
     * NOT SWALLOWED. A `base` that silently fails to persist is an infinite resync, and the only way
     * anyone finds out is by watching the upload counter start again from one.
     */
    public void saveBase(String key, Map<String, Map<String, Object>> base) throws Exception {
        File f = baseFile(key);
        File tmp = new File(f.getPath() + ".tmp");
        FileOutputStream out = new FileOutputStream(tmp);
        try {
            out.write(SyncCrypto.utf8(Json.write(base)));
            out.flush();
            out.getFD().sync();
        } finally { out.close(); }
        if (!tmp.renameTo(f)) {
            // Some filesystems refuse a rename over an existing file; delete and retry once rather
            // than leaving the old agreement in place and reporting success.
            if (!(f.delete() && tmp.renameTo(f))) {
                throw new java.io.IOException("could not store the agreement for " + key);
            }
        }
    }

    /** A path journal is resumable progress; it is not proof that the first sweep completed. */
    private String baselineKey(String key) {
        return "baseline:" + SyncCrypto.sha256hex(SyncCrypto.utf8(key));
    }

    public boolean baselineComplete(String key) {
        /* THE MARKER AND ITS AGREEMENT ARE ONE CHECKPOINT. Android can retain preferences while
         * app files are cleared/restored independently, and older builds also left this boolean
         * behind when a folder was paired again. A marker without the exact base it certifies must
         * never grant deletion authority to what is, in fact, a first sync. */
        return baseFile(key).isFile() && prefs().getBoolean(baselineKey(key), false);
    }

    public void markBaselineComplete(String key) {
        prefs().edit().putBoolean(baselineKey(key), true).commit();
    }

    public void dropBase(String key) {
        try {
            File base = baseFile(key);
            base.delete();
            new File(base.getPath() + ".state").delete();
            new File(base.getPath() + ".tmp").delete();
            new File(base.getPath() + ".state.tmp").delete();
        } catch (Exception ignored) { }
        prefs().edit().remove(baselineKey(key))
                .remove("lastSync:" + key).remove("lastFull:" + key).commit();
    }
}
