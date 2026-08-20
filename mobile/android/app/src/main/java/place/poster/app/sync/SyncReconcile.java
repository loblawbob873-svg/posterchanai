package place.poster.app.sync;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * The per-file engine, in Java — the same rules as static/js/client/syncstate.js, decision for
 * decision.
 *
 * WHY IT EXISTS TWICE. A phone must sync with its screen off, and a WebView's JavaScript is throttled
 * to about one timer a minute the moment the page is hidden — so the sweep that runs while the phone
 * is asleep cannot be the JS one. Two implementations of a rule is how a sync loops forever or
 * deletes something, so they are kept identical on purpose and
 * tests/test_android_reconcile_parity.py RUNS both against the same generated inputs and compares
 * the plans, decision for decision.
 *
 * The design, in one paragraph: THE FOLDER IS ONE VERSIONED RECORD PER FILE, and the server refuses
 * any write that is not strictly newer than the record it replaces. There is no per-device document,
 * no merge and no view that can be partial: a record set that could not be read throws before this
 * runs, a deletion is always a positive tombstone record, and a device's own past life cannot haunt
 * it because retiring a folder bumps an ERA that makes every old record unspeakable.
 */
public final class SyncReconcile {

    private SyncReconcile() { }

    /** Below this, deleting or republishing a few files is ordinary work and is not questioned. */
    public static final int FLOOR = 20;
    /* There is no separate CAP any more. It was 100, and the band between it and FLOOR is exactly
     * where a wave of stale tombstones used to cross in silence — see check(). The server keeps its
     * own backstop at 100 for a client that has gone wrong; that is a different job. */

    public static long versionOf(Map<String, Object> e) {
        if (e == null) return 0L;
        Object v = e.get("v");
        return v instanceof Number ? ((Number) v).longValue() : 0L;
    }

    private static long num(Object v) { return v instanceof Number ? ((Number) v).longValue() : 0L; }

    private static String str(Object v) { return v == null ? "" : String.valueOf(v); }

    /**
     * Did the file on disk change since this device last applied something to it?
     *
     * Against the journal's record of what the file looked like then — never against a published
     * entry, because a downloaded file gets whatever last-modified the platform gives it (SAF assigns
     * its own), so comparing the two reports every downloaded file as edited on every sweep.
     */
    public static boolean diskChanged(Map<String, Object> local, Map<String, Object> idx) {
        Object rawLocal = idx == null ? null : idx.get("local");
        Map<String, Object> had = rawLocal instanceof Map ? Json.obj(rawLocal) : null;
        if (local == null && had == null) return false;
        if (local == null) return idx != null && num(idx.get("deletedAt")) == 0;
        if (had == null) return true;
        String lc = str(local.get("csum")), hc = str(had.get("csum"));
        if (!lc.isEmpty() && !hc.isEmpty()) return !lc.equals(hc);
        long ls = num(local.get("size")), hs = num(had.get("size"));
        if (ls != hs) return true;
        return Math.abs(num(local.get("mtime")) - num(had.get("mtime"))) > SyncDiff.MTIME_SLOP;
    }

    /**
     * Did the folder's record move past what this device applied? STRICTLY AHEAD, never merely
     * different: the journal legitimately runs ahead of the record set for the length of one publish
     * (a sweep that uploaded and then failed to publish), and reading that as "changed elsewhere" is
     * the silent revert the second engine had to learn the hard way. What the journal knows and the
     * folder does not is OURS TO PUBLISH, never theirs to teach us.
     */
    public static boolean recordAhead(Map<String, Object> record, Map<String, Object> idx) {
        return record != null && versionOf(record) > versionOf(idx);
    }

    public static long bump(Map<String, Object> record, Map<String, Object> idx) {
        return Math.max(versionOf(record), versionOf(idx)) + 1;
    }

    private static boolean hasAddress(Map<String, Object> e) {
        if (e == null) return false;
        Object sha = e.get("sha");
        if (sha instanceof String && !((String) sha).isEmpty()) return true;
        Object chunks = e.get("chunks");
        return chunks instanceof List && !((List<?>) chunks).isEmpty();
    }

    /** The plan, in the same buckets and the same order as the JS engine's. */
    public static final class Plan {
        public final List<Map<String, Object>> fetch = new ArrayList<Map<String, Object>>();
        public final List<Map<String, Object>> send = new ArrayList<Map<String, Object>>();
        public final List<Map<String, Object>> trash = new ArrayList<Map<String, Object>>();
        public final List<Map<String, Object>> tombstone = new ArrayList<Map<String, Object>>();
        public final List<Map<String, Object>> keepBoth = new ArrayList<Map<String, Object>>();
        public final List<Map<String, Object>> settle = new ArrayList<Map<String, Object>>();
        public int unchanged = 0;
        public int settledGone = 0;   // deletions both sides already agree on — never "kept files"
        public int excluded = 0;

        public Map<String, Object> toMap() {
            Map<String, Object> m = new LinkedHashMap<String, Object>();
            m.put("fetch", fetch);
            m.put("send", send);
            m.put("trash", trash);
            m.put("tombstone", tombstone);
            m.put("keepBoth", keepBoth);
            m.put("settle", settle);
            m.put("unchanged", (long) unchanged);
            m.put("excluded", (long) excluded);
            return m;
        }
    }

    static Map<String, Object> act(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<String, Object>();
        for (int i = 0; i + 1 < kv.length; i += 2) m.put(String.valueOf(kv[i]), kv[i + 1]);
        return m;
    }

    public static Plan plan(Map<String, Map<String, Object>> disk,
                            Map<String, Map<String, Object>> state,
                            Map<String, Map<String, Object>> index,
                            List<String> excludes, String me, long now) {
        Plan plan = new Plan();
        SyncDiff.Excluder excluded = SyncDiff.excluder(excludes);
        Set<String> paths = new LinkedHashSet<String>();
        paths.addAll(disk.keySet());
        paths.addAll(state.keySet());
        paths.addAll(index.keySet());
        List<String> sorted = new ArrayList<String>(paths);
        Collections.sort(sorted);

        for (String path : sorted) {
            if (excluded.test(path)) { plan.excluded++; continue; }

            Map<String, Object> L = disk.get(path);
            Map<String, Object> R = state.get(path);
            Map<String, Object> idx = index.get(path);

            /* AN ADDRESS-LESS RECORD, HELD HERE, IS A SEND — an upload that died between the record
             * and its bytes strands every other device on "does not say where this file is stored"
             * while the holder settles it as unchanged. Whoever has a local copy re-publishes it. */
            if (L != null && SyncDiff.live(R) && !hasAddress(R)) {
                plan.send.add(act("path", path, "v", bump(R, idx), "stat", L,
                        "why", "the shared record names no storage — re-publishing from this copy"));
                continue;
            }

            /* A RECORD THE FOLDER LOST IS RESTORED BY WHOEVER HOLDS THE FILE. Absent this, the path
             * sits "unchanged" here for ever while no other device can learn it exists. (A lost
             * TOMBSTONE needs no restoring: no record and no file is a path nobody claims.) */
            if (R == null && idx != null && num(idx.get("deletedAt")) == 0
                    && L != null && !diskChanged(L, idx)) {
                plan.send.add(act("path", path, "v", bump(null, idx), "stat", L,
                        "why", "the folder has no record of this file — restoring it from this copy"));
                continue;
            }

            boolean here = diskChanged(L, idx);
            boolean there = recordAhead(R, idx);

            if (!here && !there) {
                plan.unchanged++;
                if (R != null && !SyncDiff.live(R)) plan.settledGone++;
                continue;
            }

            /* ---- the folder moved and this device did not: apply it. */
            if (there && !here) {
                /* Bytes we already hold are ADOPTED, not downloaded — our own publish coming back,
                 * or another device uploading a file we already have. */
                if (SyncDiff.live(R) && L != null && SyncDiff.same(L, R)) {
                    plan.settle.add(act("path", path, "v", versionOf(R), "entry", R,
                            "why", "same content both sides"));
                } else if (SyncDiff.live(R)) {
                    plan.fetch.add(act("path", path, "v", versionOf(R), "entry", R,
                            "from", str(R.get("by")),
                            "why", idx != null ? "changed elsewhere" : "new elsewhere"));
                } else if (L != null) {
                    plan.trash.add(act("path", path, "v", versionOf(R), "entry", R,
                            "to", SyncDiff.trashPath(path, now), "why", "deleted elsewhere"));
                } else {
                    plan.settle.add(act("path", path, "v", versionOf(R), "entry", R,
                            "why", "already gone here"));
                }
                continue;
            }

            /* ---- this device moved and the folder did not: publish it. */
            if (here && !there) {
                if (L != null) {
                    plan.send.add(act("path", path, "v", bump(R, idx), "stat", L,
                            "why", idx != null ? "changed here" : "new here"));
                } else {
                    plan.tombstone.add(act("path", path, "v", bump(R, idx), "why", "deleted here"));
                }
                continue;
            }

            /* ---- both moved. The first two are not conflicts at all. */
            if (L == null && SyncDiff.gone(R)) {
                plan.settle.add(act("path", path, "v", versionOf(R), "entry", R, "why", "deleted on both"));
                continue;
            }
            if (L != null && SyncDiff.live(R) && SyncDiff.same(L, R)) {
                plan.settle.add(act("path", path, "v", versionOf(R), "entry", R,
                        "why", "same content both sides"));
                continue;
            }
            // Delete loses to edit, both ways.
            if (L == null && SyncDiff.live(R)) {
                plan.fetch.add(act("path", path, "v", versionOf(R), "entry", R,
                        "from", str(R.get("by")),
                        "why", "deleted here but edited elsewhere — keeping the edit"));
                continue;
            }
            if (L != null && SyncDiff.gone(R)) {
                /* A JOINING DEVICE'S UNCHANGED COPY OBEYS THE DELETION. Tombstones keep the deleted
                 * content's csum, and a journal-less join hashes its scan — so when this local copy
                 * IS the bytes that were deliberately deleted, the deletion applies here too instead
                 * of resurrecting on every device that ever held the file. Only an actual edit wins
                 * over a delete. */
                String rc = R == null ? "" : str(R.get("csum"));
                String lc = str(L.get("csum"));
                if (!rc.isEmpty() && !lc.isEmpty() && rc.equals(lc)) {
                    plan.trash.add(act("path", path, "v", versionOf(R), "entry", R,
                            "to", SyncDiff.trashPath(path, now),
                            "why", "deleted elsewhere — this copy is the deleted version"));
                } else {
                    plan.send.add(act("path", path, "v", bump(R, idx), "stat", L,
                            "resurrect", Boolean.TRUE,
                            "why", "deleted elsewhere but edited here — keeping the edit"));
                }
                continue;
            }
            // Divergent bytes: keep both, the incoming copy takes the name, ours is renamed.
            String by = R == null ? "" : str(R.get("by"));
            long stamp = R == null ? 0 : num(R.get("mtime"));
            if (stamp == 0 && R != null) stamp = num(R.get("deletedAt"));
            plan.keepBoth.add(act("path", path, "v", versionOf(R), "entry", R,
                    "keepAs", SyncDiff.conflictPath(path, by.isEmpty() ? "another device" : by,
                            stamp != 0 ? stamp : now),
                    "why", "edited on both — the incoming copy takes the name, yours is renamed"));
        }
        return plan;
    }

    /**
     * Every rule above decides ONE path, and a bad input does not produce one bad decision — it
     * produces ten thousand identical ones. This is the only thing that looks at the SHAPE of the
     * answer, and the native sweep has nobody to ask, so every verdict here is a refusal.
     */
    public static List<Map<String, Object>> check(Plan p, Map<String, Map<String, Object>> state) {
        List<Map<String, Object>> out = new ArrayList<Map<String, Object>>();
        int settled = 0;
        for (Map<String, Object> s : p.settle) {
            if ("same content both sides".equals(str(s.get("why")))) settled++;
        }
        // Live survivors only: agreed tombstones are ballast that would eat the guard (see the JS
        // engine's comment — 50 live files beside 10,000 old deletions trashed all 50, guard silent).
        int keep = p.unchanged - p.settledGone + p.fetch.size() + p.send.size() + p.keepBoth.size() + settled;

        if (p.trash.size() >= FLOOR && p.trash.size() > keep) {
            out.add(act("kind", "massTrash", "rule", "shortList",
                        "n", (long) p.trash.size(), "keep", (long) keep));
        }
        /* THE ABSOLUTE FLOOR, AND IT IS THE SAME NUMBER IN BOTH DIRECTIONS — see the JS engine's
         * comment. Proportional is not enough on a big folder and nobody is watching: 59 stale
         * tombstones against a 1,000-file folder passed the ratio AND a cap of 100, so this device
         * trashed 59 files with no verdict, while the one device still holding them was refused by
         * the resurrect floor at 20. The asymmetry always resolved towards deleted. */
        if (p.trash.size() >= FLOOR) {
            out.add(act("kind", "massTrash", "rule", "floor",
                        "n", (long) p.trash.size(), "keep", (long) keep));
        }
        /* A DEVICE HOLDING NOTHING MAY NOT DELETE THE FOLDER — fatal, never offered. See the JS
         * engine. A scan that found NOTHING while the journal knows about hundreds of files is a
         * device that has lost sight of a folder, not one somebody emptied, and the native sweep
         * has nobody to ask in the first place: without this it simply refuses and retries for
         * ever, while the page-side sweep puts a destructive default one tap away. */
        if (p.tombstone.size() >= FLOOR && keep == 0) {
            out.add(act("kind", "massTombstone", "rule", "emptyDevice", "fatal", Boolean.TRUE,
                        "n", (long) p.tombstone.size(), "keep", (long) keep));
        }
        if (p.tombstone.size() >= FLOOR && p.tombstone.size() > keep) {
            out.add(act("kind", "massTombstone", "rule", "shortList",
                        "n", (long) p.tombstone.size(), "keep", (long) keep));
        }
        if (p.tombstone.size() >= FLOOR) {
            out.add(act("kind", "massTombstone", "rule", "floor",
                        "n", (long) p.tombstone.size(), "keep", (long) keep));
        }
        int res = 0;
        for (Map<String, Object> s : p.send) if (Boolean.TRUE.equals(s.get("resurrect"))) res++;
        if (res >= FLOOR) out.add(act("kind", "massResurrect", "rule", "floor", "n", (long) res));

        // A path that another live record sits under cannot be written as a file on any device.
        if (state != null) {
            for (Map<String, Object> a : p.fetch) {
                String path = str(a.get("path"));
                String pre = path + "/";
                for (Map.Entry<String, Map<String, Object>> q : state.entrySet()) {
                    if (!q.getKey().equals(path) && SyncDiff.live(q.getValue())
                            && q.getKey().startsWith(pre)) {
                        out.add(act("kind", "blocked", "fatal", Boolean.TRUE, "path", path));
                        break;
                    }
                }
            }
            /* TWO NAMES, ONE FILE, ON A FOLDING FILESYSTEM — mirrors the JS engine: `Photo.jpg`
             * and `photo.jpg` are one file on Windows/macOS/most of Android, and fetching both
             * makes the two records climb versions against each other for ever. Only the winner
             * (highest version, then first name) may be written; the twins are refused fatally. */
            Map<String, List<String>> groups = new LinkedHashMap<String, List<String>>();
            for (Map.Entry<String, Map<String, Object>> q : state.entrySet()) {
                if (!SyncDiff.live(q.getValue())) continue;
                String f = java.text.Normalizer.normalize(q.getKey(),
                        java.text.Normalizer.Form.NFC).toLowerCase(java.util.Locale.ROOT);
                List<String> gg = groups.get(f);
                if (gg == null) { gg = new ArrayList<String>(); groups.put(f, gg); }
                gg.add(q.getKey());
            }
            Set<String> writes = new LinkedHashSet<String>();
            for (Map<String, Object> a : p.fetch) writes.add(str(a.get("path")));
            for (Map<String, Object> a : p.keepBoth) writes.add(str(a.get("path")));
            for (List<String> twins : groups.values()) {
                if (twins.size() < 2) continue;
                final Map<String, Map<String, Object>> st = state;
                Collections.sort(twins, new java.util.Comparator<String>() {
                    public int compare(String x, String y) {
                        long d = versionOf(st.get(y)) - versionOf(st.get(x));
                        if (d != 0) return d > 0 ? 1 : -1;
                        return x.compareTo(y);
                    }
                });
                for (int i = 1; i < twins.size(); i++) {
                    if (writes.contains(twins.get(i))) {
                        out.add(act("kind", "blocked", "fatal", Boolean.TRUE, "path", twins.get(i)));
                    }
                }
            }
        }
        return out;
    }

    /**
     * The plan with what a refusal forbids taken out — and nothing else.
     *
     * REFUSING SUPPRESSES ONE KIND OF ACTION, NEVER THE SWEEP. A guard that aborts everything is the
     * same bug with its sign flipped, which is what happened to the contacts sweep: it stopped
     * syncing altogether rather than stopping the deletions.
     */
    public static Plan apply(Plan p, List<Map<String, Object>> verdicts) {
        Plan out = new Plan();
        out.unchanged = p.unchanged;
        out.settledGone = p.settledGone;
        out.excluded = p.excluded;
        boolean noTrash = false, noTomb = false, noRes = false;
        Set<String> blocked = new LinkedHashSet<String>();
        for (Map<String, Object> v : verdicts) {
            String k = str(v.get("kind"));
            if ("massTrash".equals(k)) noTrash = true;
            else if ("massTombstone".equals(k)) noTomb = true;
            else if ("massResurrect".equals(k)) noRes = true;
            else if ("blocked".equals(k)) blocked.add(str(v.get("path")));
        }
        for (Map<String, Object> f : p.fetch) {
            if (!blocked.contains(str(f.get("path")))) out.fetch.add(f);
        }
        for (Map<String, Object> f : p.keepBoth) {
            if (!blocked.contains(str(f.get("path")))) out.keepBoth.add(f);
        }
        out.settle.addAll(p.settle);
        if (!noTrash) out.trash.addAll(p.trash);
        if (!noTomb) out.tombstone.addAll(p.tombstone);
        for (Map<String, Object> s : p.send) {
            if (noRes && Boolean.TRUE.equals(s.get("resurrect"))) continue;
            out.send.add(s);
        }
        return out;
    }
}
