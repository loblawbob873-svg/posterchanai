package place.poster.app.sync;

import java.util.ArrayList;
import java.util.Calendar;
import java.util.GregorianCalendar;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.TimeZone;
import java.util.regex.Pattern;
import java.util.regex.PatternSyntaxException;

/**
 * The decision engine, in Java: a line-by-line port of `static/js/client/foldersync.js`.
 *
 * THIS IS THE CODE THAT DECIDES WHETHER FILES GET DELETED, so it is a port and not an
 * interpretation. Every rule the JavaScript carries — and each of them is there because it once cost
 * somebody data — has to hold identically here, or a phone syncing in the background will disagree
 * with the same account's laptop about what a folder contains:
 *
 *   * three snapshots, because local+remote alone cannot tell "new here" from "deleted there";
 *   * a conflict keeps BOTH copies and never picks a winner;
 *   * delete loses to edit, in both directions — except against a tombstone newer than the local
 *     copy, which is a device that lost its agreement rather than one that edited anything;
 *   * a deleted file is absent from a scan AND a tombstone in `base`, and those are the same state,
 *     or every deletion re-proposes itself on every sweep for ever;
 *   * excluding a folder drops it from ALL THREE snapshots, so it is never DELETED elsewhere;
 *   * a short list is a delete order: refuse to trash more than you keep, above a floor;
 *   * and the mirror of it, refuse to republish twenty deletions a restored machine thinks are edits.
 *
 * `tests/test_android_native_sync_diff.py` does not check that against a description either: it
 * drives the SAME scenarios through node running the shipped JavaScript and through this, and
 * compares the plans.
 *
 * Pure. No Android, no I/O, no network — the executor is elsewhere, exactly as on the JS side.
 */
public final class SyncDiff {

    private SyncDiff() { }

    /** ms. FAT32/SMB/Android SAF round mtimes; exFAT to 2s. Tighter reports every file as changed. */
    public static final long MTIME_SLOP = 2000;
    public static final int MASS_DELETE_FLOOR = 20;
    public static final int MASS_RESURRECT_FLOOR = 20;

    // ------------------------------------------------------------------------ content identity

    private static Long numOrNull(Object v) {
        if (v instanceof Long) return (Long) v;
        if (v instanceof Double) return (long) (double) (Double) v;
        return null;
    }

    private static long deletedAt(Map<String, Object> e) {
        if (e == null) return 0;
        Long v = numOrNull(e.get("deletedAt"));
        return v == null ? 0 : v;
    }

    private static String text(Map<String, Object> e, String k) {
        if (e == null) return "";
        Object v = e.get(k);
        return v instanceof String ? (String) v : "";
    }

    private static List<Object> chunks(Map<String, Object> e) {
        if (e == null) return null;
        Object v = e.get("chunks");
        return v instanceof List ? Json.arr(v) : null;
    }

    /** An entry that names actual bytes. A `deletedAt` of 0 is live, as it is in JS. */
    public static boolean live(Map<String, Object> e) { return e != null && deletedAt(e) == 0; }

    public static boolean gone(Map<String, Object> e) { return e == null || deletedAt(e) != 0; }

    /**
     * Is this the same content?
     *
     * `csum` is the FILE's hash; `sha` is where its bytes are STORED (the hash of the ciphertext).
     * They are different numbers and comparing one against the other is how a folder duplicates
     * itself, so content identity is csum, or the chunk list at the same chunk size, and otherwise
     * size+mtime — which is what an ordinary sweep has, because rehashing a 40GB folder every time
     * is a space heater rather than a background task.
     */
    public static boolean same(Map<String, Object> a, Map<String, Object> b) {
        if (a == null || b == null) return a == null && b == null;
        if (deletedAt(a) != 0 || deletedAt(b) != 0) return (deletedAt(a) != 0) == (deletedAt(b) != 0);
        String ca = text(a, "csum"), cb = text(b, "csum");
        if (!ca.isEmpty() && !cb.isEmpty()) return ca.equals(cb);
        /* An empty chunk list is TRUTHY in JS, so `chunks: []` on both sides compares equal there and
         * must here — an `isEmpty()` guard reads like tidiness and is a behaviour change. Chunk lists
         * identify content only at the SAME chunk size, which is what `cs` records. */
        List<Object> ka = chunks(a), kb = chunks(b);
        if (ka != null && kb != null && csize(a) == csize(b)) {
            if (ka.size() != kb.size()) return false;
            for (int i = 0; i < ka.size(); i++) {
                Object x = ka.get(i), y = kb.get(i);
                if (x == null ? y != null : !x.equals(y)) return false;
            }
            return true;
        }
        Long sa = numOrNull(a.get("size")), sb = numOrNull(b.get("size"));
        boolean sizeEq = sa == null ? sb == null : sa.equals(sb);   // JS: undefined === undefined is true
        long ma = or0(a.get("mtime")), mb = or0(b.get("mtime"));
        return sizeEq && Math.abs(ma - mb) <= MTIME_SLOP;
    }

    private static long csize(Map<String, Object> e) {
        Long v = numOrNull(e.get("cs"));
        return v == null ? 0 : v;
    }

    private static long or0(Object v) {
        Long n = numOrNull(v);
        return n == null ? 0 : n;
    }

    // ------------------------------------------------------------------------------- naming

    private static String utcDay(long when) {
        Calendar c = new GregorianCalendar(TimeZone.getTimeZone("UTC"));
        c.setTimeInMillis(when);
        return String.format(Locale.US, "%04d-%02d-%02d", c.get(Calendar.YEAR),
                             c.get(Calendar.MONTH) + 1, c.get(Calendar.DAY_OF_MONTH));
    }

    /**
     * `dir/name.ext` → `dir/name (conflict from laptop, 2026-08-09).ext`. The suffix goes BEFORE the
     * extension, so the copy still opens in whatever owns that type.
     */
    public static String conflictPath(String path, String device, long when) {
        int slash = path.lastIndexOf('/');
        String dir = slash < 0 ? "" : path.substring(0, slash + 1);
        String name = slash < 0 ? path : path.substring(slash + 1);
        int dot = name.lastIndexOf('.');
        String stem = dot > 0 ? name.substring(0, dot) : name;
        String ext = dot > 0 ? name.substring(dot) : "";
        return dir + stem + " (conflict from " + (device == null || device.isEmpty() ? "another device" : device)
               + ", " + utcDay(when) + ")" + ext;
    }

    /** Dated, so two deletions of the same name a week apart do not collide inside the trash. */
    public static String trashPath(String path, long when) {
        return ".pc-trash/" + utcDay(when) + "/" + path;
    }

    // ---------------------------------------------------------------------------- exclusions

    /**
     * "all of Pictures except Old". Matched against the path relative to the folder AND every
     * ancestor directory of it, so `Old` catches `Old/2019/img.jpg`.
     *
     * A pattern that will not compile is DROPPED here rather than thrown, which is a deliberate
     * difference from the browser: an exclusion can only ever stop a path being looked at, never
     * delete one, so the worst a dropped pattern can do is sync a folder somebody wanted skipped —
     * where a throw would take the whole background sweep down with nothing on screen to say why.
     */
    public interface Excluder { boolean test(String path); }

    static Pattern rx(String pattern) {
        String p = pattern == null ? "" : pattern.trim().replace("\\", "/");
        while (p.endsWith("/")) p = p.substring(0, p.length() - 1);
        if (p.isEmpty()) return null;
        boolean anchored = p.startsWith("/");
        if (anchored) p = p.substring(1);
        // `**/cache` means "cache at any depth, INCLUDING the top" — gitignore's reading. Unanchored
        // already means any depth, so the prefix is simply dropped.
        else if (p.startsWith("**/")) p = p.substring(3);
        StringBuilder body = new StringBuilder();
        for (int i = 0; i < p.length(); i++) {
            char c = p.charAt(i);
            if (".+^${}()|[]\\".indexOf(c) >= 0) { body.append('\\').append(c); continue; }
            if (c == '*') {
                if (i + 1 < p.length() && p.charAt(i + 1) == '*') { body.append(".*"); i++; }
                else body.append("[^/]*");
                continue;
            }
            body.append(c);
        }
        try {
            /* UNICODE_CASE, not CASE_INSENSITIVE alone. On its own Java folds ASCII and nothing
             * else, while JavaScript's `i` flag folds Unicode — so a folder the user typed as
             * `Übungen` and the disk spells `übungen` is excluded by the browser and NOT by the
             * phone, and the two devices then sync different sets from the same exclusion list. In
             * a file whose whole contract is "the same answer as foldersync.js". */
            return Pattern.compile("^" + (anchored ? "" : "(?:.*/)?") + body + "$",
                                   Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE);
        } catch (PatternSyntaxException e) {
            return null;
        }
    }

    public static Excluder excluder(List<String> patterns) {
        final List<Pattern> rx = new ArrayList<Pattern>();
        if (patterns != null) for (String p : patterns) {
            Pattern c = rx(p);
            if (c != null) rx.add(c);
        }
        if (rx.isEmpty()) return new Excluder() { public boolean test(String path) { return false; } };
        return new Excluder() {
            public boolean test(String path) {
                String[] parts = (path == null ? "" : path).split("/", -1);
                for (int i = parts.length; i > 0; i--) {
                    StringBuilder sub = new StringBuilder();
                    for (int j = 0; j < i; j++) {
                        if (j > 0) sub.append('/');
                        sub.append(parts[j]);
                    }
                    String s = sub.toString();
                    for (Pattern r : rx) if (r.matcher(s).matches()) return true;
                }
                return false;
            }
        };
    }

    // --------------------------------------------------------------------------------- diff

    /* THE PLAN AND THE THREE-WAY DIFF MOVED TO SyncReconcile.java, and the move was not a tidy-up.
     *
     * They decided against ONE shared manifest — a document every device read, edited and wrote back
     * — which is last-writer-wins on the record of whether your files exist. Every device publishes
     * its own document now and the folder is the merge of them, so the question this code answered
     * ("the entry is missing from the manifest, does that mean deleted?") does not arise: a deletion
     * is a tombstone somebody published, and absence is absence.
     *
     * What stays here is what was never wrong and is used by both: content identity, the exclusion
     * matcher, conflict and trash naming, and the battery policy below.
     */

    public static Map<String, Object> shouldSync(Map<String, Object> state, Map<String, Object> prefs) {
        Map<String, Object> s = state == null ? new LinkedHashMap<String, Object>() : state;
        Map<String, Object> p = prefs == null ? new LinkedHashMap<String, Object>() : prefs;

        if (!Json.bool(p.get("enabled"), true)) return say("none", "sync is off for this folder");
        // Pressing the button beats every constraint below — this is somebody standing there having
        // just asked, and refusing them because the battery is at 19% is how a feature earns a
        // reputation. But it asks for a SYNC, not a rehash of the whole folder.
        if (Json.bool(s.get("manual"), false)) {
            return say(Json.bool(s.get("deep"), false) ? "full" : "incremental", "you asked for it");
        }
        // A FOLDER DOES NOT START ON ITS OWN: the first sweep is the expensive one and the one that
        // publishes a folder's whole contents, so it is the worst thing to begin by accident.
        if (Json.bool(p.get("paused"), false)) {
            return say("none", "not started yet — set what to exclude, then press Start");
        }
        boolean chargingKnown = s.get("charging") != null;
        boolean charging = Json.bool(s.get("charging"), false);
        if (chargingKnown && !charging && Json.bool(p.get("onlyWhenCharging"), false)) {
            return say("none", "waiting until you plug in");
        }
        if (Json.bool(s.get("metered"), false) && Json.bool(p.get("wifiOnly"), true)) {
            return say("none", "waiting for Wi-Fi");
        }
        if (s.get("online") != null && !Json.bool(s.get("online"), true)) return say("none", "offline");

        long battery = s.get("battery") instanceof Long || s.get("battery") instanceof Double
                ? Json.num(s.get("battery"), 100) : 100;
        long minBattery = Json.num(p.get("minBattery"), 20);
        if (!charging && battery < minBattery) {
            return say("metadata", "battery at " + battery + "% — noting changes, uploading later");
        }
        long now = Json.num(s.get("now"), 0), lastSync = Json.num(s.get("lastSyncAt"), 0);
        long minInterval = Json.num(p.get("minIntervalMs"), 15 * 60 * 1000L);
        if (lastSync != 0 && (now - lastSync) < minInterval && !Json.bool(s.get("dirty"), false)) {
            return say("none", "nothing changed since the last sweep");
        }
        long sinceFull = now - Json.num(s.get("lastFullScanAt"), 0);
        long fullEvery = Json.num(p.get("fullScanIntervalMs"), 24 * 60 * 60 * 1000L);
        if (charging && sinceFull >= fullEvery) {
            return say("full", "plugged in, and it has been a while since a full check");
        }
        return say("incremental", charging ? "plugged in" : "on battery — changed files only");
    }

    private static Map<String, Object> say(String mode, String why) {
        Map<String, Object> m = new LinkedHashMap<String, Object>();
        m.put("mode", mode);
        m.put("why", why);
        m.put("run", !"none".equals(mode));
        return m;
    }

    /**
     * Fold a completed plan back into the agreement that becomes the next run's `base`. Here, with
     * the rules it has to agree with, rather than in each executor — two implementations of "what did
     * we just agree to" is how a sync loops for ever, re-uploading what it just downloaded.
     */
    public static Map<String, Map<String, Object>> advance(Map<String, Map<String, Object>> base,
                                                           Map<String, Map<String, Object>> done,
                                                           List<String> removed, long now) {
        Map<String, Map<String, Object>> out = new LinkedHashMap<String, Map<String, Object>>();
        if (base != null) out.putAll(base);
        if (done != null) for (Map.Entry<String, Map<String, Object>> e : done.entrySet()) {
            out.put(e.getKey(), new LinkedHashMap<String, Object>(e.getValue()));
        }
        if (removed != null) for (String path : removed) {
            Map<String, Object> t = new LinkedHashMap<String, Object>();
            t.put("deletedAt", now);
            out.put(path, t);
        }
        return out;
    }
}
