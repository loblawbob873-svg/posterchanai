package place.poster.app.sync;

import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * The reconciler, in Java — the same rules as static/js/client/syncengine.js, decision for decision.
 *
 * WHY IT EXISTS TWICE. A phone must sync with its screen off, and a WebView's JavaScript is throttled
 * to about one timer a minute the moment the page is hidden — so the sweep that runs while the phone
 * is asleep cannot be the JS one. Two implementations of a rule is how a sync loops forever or
 * deletes something, so they are kept identical on purpose and
 * tests/test_android_reconcile_parity.py RUNS both against the same generated inputs and compares
 * the plans, decision for decision.
 *
 * The design, in one paragraph: every device publishes its OWN document and nothing else ever writes
 * it, the folder is the merge of those documents, and each entry carries a version counter that a
 * device raises when it publishes a change. Two devices syncing at once therefore cannot overwrite
 * each other's record, and no single document — missing, unreadable or wrong — can make the folder
 * look empty. Absence is never a deletion; a deletion is a tombstone somebody published.
 */
public final class SyncReconcile {

    private SyncReconcile() { }

    /** Below this, deleting or republishing a few files is ordinary work and is not questioned. */
    public static final int FLOOR = 20;

    public static long versionOf(Map<String, Object> e) {
        if (e == null) return 0L;
        Object v = e.get("v");
        return v instanceof Number ? ((Number) v).longValue() : 0L;
    }

    private static long stampOf(Map<String, Object> e) {
        if (e == null) return 0L;
        long d = num(e.get("deletedAt"));
        return d != 0 ? d : num(e.get("mtime"));
    }

    private static long num(Object v) { return v instanceof Number ? ((Number) v).longValue() : 0L; }

    private static String str(Object v) { return v == null ? "" : String.valueOf(v); }

    /** The merged folder, plus the losing claim wherever two devices published the same version. */
    public static final class Merged {
        public final Map<String, Map<String, Object>> global = new LinkedHashMap<String, Map<String, Object>>();
        public final Map<String, Map<String, Object>> rivals = new LinkedHashMap<String, Map<String, Object>>();
        public final Map<String, String> rivalBy = new LinkedHashMap<String, String>();
        public final Map<String, String> by = new LinkedHashMap<String, String>();
        public final List<String> devices = new ArrayList<String>();
    }

    /**
     * Merge every device's view.
     *
     * Version first, then the entry's own timestamp (which is what orders a pair that has not
     * published a version yet), then the device id — which decides nothing meaningful and is there so
     * every device reaches the SAME answer. A merge that is not deterministic is a folder that
     * flickers between two states as the devices take turns.
     */
    public static Merged merge(Map<String, Map<String, Map<String, Object>>> views) {
        Merged m = new Merged();
        List<String> devs = new ArrayList<String>(views == null ? Collections.<String>emptySet() : views.keySet());
        Collections.sort(devs);
        m.devices.addAll(devs);
        for (String dev : devs) {
            Map<String, Map<String, Object>> view = views.get(dev);
            if (view == null) continue;
            for (Map.Entry<String, Map<String, Object>> e : view.entrySet()) {
                String path = e.getKey();
                Map<String, Object> claim = e.getValue();
                Map<String, Object> cur = m.global.get(path);
                if (cur == null) { m.global.put(path, claim); m.by.put(path, dev); continue; }
                String curBy = m.by.get(path);
                boolean claimWins = laterThan(claim, dev, cur, curBy);
                Map<String, Object> win = claimWins ? claim : cur;
                Map<String, Object> lose = claimWins ? cur : claim;
                String winBy = claimWins ? dev : curBy;
                String loseBy = claimWins ? curBy : dev;
                m.global.put(path, win);
                m.by.put(path, winBy);
                /* A RIVAL IS A CONCURRENT EDIT, not an out-of-date copy: same version, different
                 * content means two devices changed this without either seeing the other. */
                if (versionOf(win) == versionOf(lose) && !SyncDiff.same(win, lose)) {
                    m.rivals.put(path, lose);
                    m.rivalBy.put(path, loseBy);
                }
            }
        }
        return m;
    }

    private static boolean laterThan(Map<String, Object> a, String aBy, Map<String, Object> b, String bBy) {
        long va = versionOf(a), vb = versionOf(b);
        if (va != vb) return va > vb;
        long sa = stampOf(a), sb = stampOf(b);
        if (sa != sb) return sa > sb;
        return str(aBy).compareTo(str(bBy)) > 0;
    }

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
     * Did the folder's own record change since this device last applied it?
     *
     * ABSENCE IS NOT NEWS. A path nobody currently claims means exactly that; it does NOT mean
     * deleted. A deletion is a tombstone, published by the device that made it. The old shape could
     * not tell those apart — there was one document and a path missing from it was the only way a
     * delete could look — which is why a document that failed to load, or came back empty, read as
     * "every file you have was deleted".
     */
    public static boolean viewChanged(Map<String, Object> remote, Map<String, Object> idx) {
        if (remote == null) return false;
        long vr = versionOf(remote), vi = versionOf(idx);
        // Strictly ahead, never merely different: a journal ahead of the merge is OURS TO PUBLISH.
        // Read as remote news it fetched old bytes back over an edit (see the JS engine's comment).
        if (vr != 0 || vi != 0) return vr > vi;
        if (idx == null) return true;
        return !SyncDiff.same(remote, idx);
    }

    public static long bump(Map<String, Object> remote, Map<String, Object> idx) {
        return Math.max(versionOf(remote), versionOf(idx)) + 1;
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

    private static Map<String, Object> act(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<String, Object>();
        for (int i = 0; i + 1 < kv.length; i += 2) m.put(String.valueOf(kv[i]), kv[i + 1]);
        return m;
    }

    public static Plan reconcile(Map<String, Map<String, Object>> disk,
                                 Merged m,
                                 Map<String, Map<String, Object>> index,
                                 List<String> excludes, String me, long now) {
        Plan plan = new Plan();
        SyncDiff.Excluder excluded = SyncDiff.excluder(excludes);
        Set<String> paths = new LinkedHashSet<String>();
        paths.addAll(disk.keySet());
        paths.addAll(m.global.keySet());
        paths.addAll(index.keySet());
        List<String> sorted = new ArrayList<String>(paths);
        Collections.sort(sorted);

        for (String path : sorted) {
            if (excluded.test(path)) { plan.excluded++; continue; }

            Map<String, Object> L = disk.get(path);
            Map<String, Object> R = m.global.get(path);
            Map<String, Object> idx = index.get(path);
            Map<String, Object> rival = m.rivals.get(path);

            /* Two other devices changed this at the same time. Both sets of bytes are stored and both
             * claims are content-addressed, so every device can make the identical repair without
             * asking anyone — which is what stops three devices each picking a different winner. */
            /* Gated exactly like the JS engine (see its comment): only a device holding a local
             * copy that is not already the winner, and whose journal has not already resolved this,
             * keeps both. Ungated, an already-resolved device re-resolved on every sweep while the
             * loser stayed offline, and a device with no local copy failed the move for ever. */
            if (rival != null && SyncDiff.live(R) && SyncDiff.live(rival)
                    && L != null && !SyncDiff.same(L, R)
                    && !(idx != null && SyncDiff.same(idx, R) && !diskChanged(L, idx))) {
                plan.keepBoth.add(act("path", path, "v", versionOf(R), "entry", R, "rival", rival,
                        "keepAs", SyncDiff.conflictPath(path, str(m.rivalBy.get(path)),
                                stampOf(rival) != 0 ? stampOf(rival) : now),
                        "why", "two devices changed this at the same time — both copies kept"));
                continue;
            }

            boolean here = diskChanged(L, idx);
            boolean there = viewChanged(R, idx);

            if (!here && !there) {
                plan.unchanged++;
                if (R != null && !SyncDiff.live(R)) plan.settledGone++;
                continue;
            }

            if (there && !here) {
                if (SyncDiff.live(R)) {
                    plan.fetch.add(act("path", path, "v", versionOf(R), "entry", R,
                            "from", str(m.by.get(path)),
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

            if (here && !there) {
                if (L != null) {
                    plan.send.add(act("path", path, "v", bump(R, idx), "stat", L,
                            "why", idx != null ? "changed here" : "new here"));
                } else {
                    plan.tombstone.add(act("path", path, "v", bump(R, idx), "why", "deleted here"));
                }
                continue;
            }

            // Both moved. The first two are not conflicts at all.
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
                        "from", str(m.by.get(path)),
                        "why", "deleted here but edited elsewhere — keeping the edit"));
                continue;
            }
            if (L != null && SyncDiff.gone(R)) {
                plan.send.add(act("path", path, "v", bump(R, idx), "stat", L, "resurrect", Boolean.TRUE,
                        "why", "deleted elsewhere but edited here — keeping the edit"));
                continue;
            }
            String by = m.by.get(path);
            plan.keepBoth.add(act("path", path, "v", versionOf(R), "entry", R,
                    "keepAs", SyncDiff.conflictPath(path, by == null || by.isEmpty() ? "another device" : by,
                            stampOf(R) != 0 ? stampOf(R) : now),
                    "why", "edited on both — the incoming copy takes the name, yours is renamed"));
        }
        return plan;
    }

    /**
     * Every rule above decides ONE path, and a bad input does not produce one bad decision — it
     * produces ten thousand identical ones. This is the only thing that looks at the SHAPE of the
     * answer, and the native sweep has nobody to ask, so every verdict here is a refusal.
     */
    public static List<Map<String, Object>> check(Plan p, int missingViews) {
        List<Map<String, Object>> out = new ArrayList<Map<String, Object>>();
        int settled = 0;
        for (Map<String, Object> s : p.settle) {
            if ("same content both sides".equals(str(s.get("why")))) settled++;
        }
        // Live survivors only: agreed tombstones are ballast that would eat the guard (see the JS
        // engine's comment — 50 live files beside 10,000 old deletions trashed all 50, guard silent).
        int keep = p.unchanged - p.settledGone + p.fetch.size() + p.send.size() + p.keepBoth.size() + settled;

        if (missingViews > 0) {
            if (!p.trash.isEmpty()) out.add(act("kind", "partialViews", "n", (long) p.trash.size()));
            if (!p.tombstone.isEmpty()) out.add(act("kind", "partialViewsOut", "n", (long) p.tombstone.size()));
        }
        if (p.trash.size() >= FLOOR && p.trash.size() > keep) {
            out.add(act("kind", "massTrash", "n", (long) p.trash.size(), "keep", (long) keep));
        }
        if (p.tombstone.size() >= FLOOR && p.tombstone.size() > keep) {
            out.add(act("kind", "massTombstone", "n", (long) p.tombstone.size(), "keep", (long) keep));
        }
        int res = 0;
        for (Map<String, Object> s : p.send) if (Boolean.TRUE.equals(s.get("resurrect"))) res++;
        if (res >= FLOOR) out.add(act("kind", "massResurrect", "n", (long) res));
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
        for (Map<String, Object> v : verdicts) {
            String k = str(v.get("kind"));
            if ("massTrash".equals(k) || "partialViews".equals(k)) noTrash = true;
            else if ("massTombstone".equals(k) || "partialViewsOut".equals(k)) noTomb = true;
            else if ("massResurrect".equals(k)) noRes = true;
        }
        out.fetch.addAll(p.fetch);
        out.keepBoth.addAll(p.keepBoth);
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
