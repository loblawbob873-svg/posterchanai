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
            return Pattern.compile("^" + (anchored ? "" : "(?:.*/)?") + body + "$",
                                   Pattern.CASE_INSENSITIVE);
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

    /** The plan. Every action names a path and says WHY, so a panel can show sentences. */
    public static final class Plan {
        public final List<Map<String, Object>> upload = new ArrayList<Map<String, Object>>();
        public final List<Map<String, Object>> download = new ArrayList<Map<String, Object>>();
        public final List<Map<String, Object>> deleteLocal = new ArrayList<Map<String, Object>>();
        public final List<Map<String, Object>> deleteRemote = new ArrayList<Map<String, Object>>();
        public final List<Map<String, Object>> conflicts = new ArrayList<Map<String, Object>>();
        public final List<Map<String, Object>> notes = new ArrayList<Map<String, Object>>();
        public int unchanged = 0;
        public int excluded = 0;

        /** The same shape `diff()` returns in JS, in the same order, so the two can be compared. */
        public Map<String, Object> toMap() {
            Map<String, Object> m = new LinkedHashMap<String, Object>();
            m.put("upload", upload);
            m.put("download", download);
            m.put("deleteLocal", deleteLocal);
            m.put("deleteRemote", deleteRemote);
            m.put("conflicts", conflicts);
            m.put("unchanged", (long) unchanged);
            m.put("excluded", (long) excluded);
            m.put("notes", notes);
            return m;
        }
    }

    private static Map<String, Object> act(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<String, Object>();
        for (int i = 0; i + 1 < kv.length; i += 2) m.put(String.valueOf(kv[i]), kv[i + 1]);
        return m;
    }

    /** `sha` when there is one, and NOTHING when there is not — JSON.stringify drops undefined. */
    private static Object shaOf(Map<String, Object> e) {
        Object v = e == null ? null : e.get("sha");
        return v == null ? Json.UNDEFINED : v;
    }

    public static Plan diff(Map<String, Map<String, Object>> local,
                            Map<String, Map<String, Object>> remote,
                            Map<String, Map<String, Object>> base,
                            List<String> excludes, String device, long now) {
        Plan plan = new Plan();
        Excluder isExcluded = excluder(excludes);
        Set<String> paths = new LinkedHashSet<String>();
        if (local != null) paths.addAll(local.keySet());
        if (remote != null) paths.addAll(remote.keySet());
        if (base != null) paths.addAll(base.keySet());
        List<String> sorted = new ArrayList<String>(paths);
        java.util.Collections.sort(sorted);        // UTF-16 code units, exactly as Array.sort() does

        for (String path : sorted) {
            if (isExcluded.test(path)) { plan.excluded++; continue; }
            Map<String, Object> L = local == null ? null : local.get(path);
            Map<String, Object> R = remote == null ? null : remote.get(path);
            Map<String, Object> B = base == null ? null : base.get(path);

            /* A DELETED FILE IS ABSENT FROM A SCAN AND A TOMBSTONE IN `base`, AND THOSE ARE THE SAME
             * STATE. Comparing them with same() answers "changed here" on every sweep for ever, and
             * the manifest rewrites itself in a loop with a count that never moves — which looks
             * healthy. Fixed here rather than in same(), because absence means two different things
             * to the two sides: no LOCAL entry is "not on this disk", no BASE entry is "this device
             * never agreed anything about this path", which is not "it agreed the path was deleted". */
            boolean localChanged = (gone(L) && gone(B)) ? false : !same(L, B);
            boolean remoteChanged = !same(R, B);

            if (!localChanged && !remoteChanged) { plan.unchanged++; continue; }

            if (localChanged && !remoteChanged) {
                if (live(L)) plan.upload.add(act("path", path, "why", live(B) ? "changed here" : "new here"));
                else plan.deleteRemote.add(act("path", path, "why", "deleted here"));
                continue;
            }
            if (remoteChanged && !localChanged) {
                if (live(R)) {
                    plan.download.add(act("path", path, "sha", shaOf(R),
                                          "why", live(B) ? "changed elsewhere" : "new elsewhere"));
                } else if (live(L)) {
                    plan.deleteLocal.add(act("path", path, "why", "deleted elsewhere"));
                }
                continue;
            }

            // Both moved.
            if (gone(L) && gone(R)) { plan.notes.add(act("path", path, "why", "deleted on both")); continue; }

            /* Converged by accident — the same edit twice, or the same file copied in on both devices.
             * same(), NOT a sha comparison: an ordinary sweep does not hash, so requiring one decides
             * "divergent" for every path, which is exactly what an EMPTY base produces. The whole
             * folder would conflict and duplicate itself on every device. */
            if (live(L) && live(R) && same(L, R)) {
                plan.notes.add(act("path", path, "why", "same content both sides"));
                continue;
            }

            if (gone(L) && live(R)) {
                plan.download.add(act("path", path, "sha", shaOf(R),
                                      "why", "deleted here but edited elsewhere — keeping the edit"));
                continue;
            }
            if (live(L) && gone(R)) {
                /* A TOMBSTONE IS NOT AN EDIT, AND HAVING A FILE IS NOT HAVING EDITED IT. We only get
                 * here with no `base` — a reinstall, "Stop syncing" and back — so this device has not
                 * edited anything, it simply cannot remember. Reading that as an edit made it upload
                 * the whole folder back over other people's deletions. A deletion recorded AFTER this
                 * copy was written stands; a local file newer than the tombstone is a real
                 * post-delete edit and still wins. */
                if (B == null && deletedAt(R) > or0(L.get("mtime")) + MTIME_SLOP) {
                    plan.deleteLocal.add(act("path", path,
                                             "why", "deleted elsewhere after this copy was written"));
                    continue;
                }
                /* `resurrect` IS A FLAG, NOT A PHRASE — this action undoes a deliberate deletion and
                 * the guard that counts them must not be a substring match on a sentence somebody
                 * could reword. */
                plan.upload.add(act("path", path, "resurrect", Boolean.TRUE,
                                    "why", "deleted elsewhere but edited here — keeping the edit"));
                continue;
            }

            String to = conflictPath(path, text(R, "device").isEmpty() ? "another device" : text(R, "device"),
                                     or0(R.get("mtime")) != 0 ? or0(R.get("mtime")) : now);
            plan.conflicts.add(act("path", path, "keepAs", to, "sha", shaOf(R),
                                   "why", "edited on both — the incoming copy takes the name, yours is renamed"));
        }
        return plan;
    }

    // ------------------------------------------------------------------------------- guards

    /**
     * A SHORT LIST IS A DELETE ORDER. Every rule above decides one path and each is right; what
     * nothing looked at was the SHAPE of the answer, and a manifest that has gone wrong does not
     * produce one bad decision but ten thousand identical ones. Measured: a shared manifest of ~10k
     * paths, every one a tombstone, took a whole Pictures folder to the trash — correctly, per path.
     *
     * The server's collapse guard cannot see it, because a mass LOCAL delete writes no manifest at
     * all. Below the floor this never asks: "delete the 3 files I deleted on my phone" is the normal
     * working of the feature, and a question about those trains people to click through this one.
     *
     * Returns null when there is nothing to ask about.
     */
    public static Map<String, Object> massDelete(Plan p) {
        if (p == null) return null;
        int n = p.deleteLocal.size();
        if (n < MASS_DELETE_FLOOR) return null;
        int settled = 0;
        for (Map<String, Object> note : p.notes) {
            if ("same content both sides".equals(Json.str(note.get("why"), ""))) settled++;
        }
        int keep = p.unchanged + p.upload.size() + p.download.size() + p.conflicts.size() + settled;
        if (n <= keep) return null;
        return act("n", (long) n, "keep", (long) keep,
                   "why", "this sweep would move " + n + " file" + (n == 1 ? "" : "s")
                          + " to the trash and keep " + keep);
    }

    /**
     * …AND THE SAME QUESTION POINTING THE OTHER WAY. `delete loses to edit` republishes a deleted
     * file whenever it looks changed here, and on an ordinary sweep "changed" is size+mtime — so a
     * device whose timestamps moved under it (a restore, a copy, rsync without -t) resurrects every
     * deletion at once.
     *
     * AN ABSOLUTE FLOOR, NOT A RATIO: a restore makes everything look edited, so the resurrections
     * arrive beside thousands of ordinary uploads and 3,930-beside-11,884 sails past any ratio.
     */
    public static Map<String, Object> massResurrect(Plan p) {
        if (p == null) return null;
        int n = 0;
        for (Map<String, Object> u : p.upload) if (Json.bool(u.get("resurrect"), false)) n++;
        if (n < MASS_RESURRECT_FLOOR) return null;
        return act("n", (long) n, "why", "this sweep would republish " + n + " file" + (n == 1 ? "" : "s")
                                         + " your other devices deleted");
    }

    // ------------------------------------------------------------------------------ the policy

    /**
     * May a sweep run right now, and how much of one — the port of foldersync.js `shouldSync`.
     *
     * IT HAS TO BE THE SAME ANSWER AS THE BROWSER'S, because the two run against the same folder and
     * the same switches. "Only when plugged in" and "Wi-Fi only" are the two the user can see, and a
     * native sweep that read them differently would either spend somebody's data plan or stop syncing
     * for a reason nothing reports.
     *
     * `mode` is the output rather than a boolean: a device on battery can still afford to notice a
     * change and upload a small document, while rehashing a Pictures folder is a charging-time job.
     * The native caller only ever asks for the automatic modes — a manual sweep is the page's.
     *
     * @param state {charging, metered, online, battery, now, lastSyncAt, lastFullScanAt, dirty, manual, deep}
     * @param prefs {enabled, paused, onlyWhenCharging, wifiOnly, minBattery, minIntervalMs, fullScanIntervalMs}
     * @return {mode, why, run}
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
