package place.poster.app.sync;

import android.content.Context;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * A folder sweep with no WebView in it.
 *
 * WHY THIS EXISTS. Chromium throttles a hidden page's JavaScript however awake the processor is —
 * a browser policy, not a power one — so the alarm, the wake lock, its renewal and resumeTimers were
 * all necessary and none of them could touch the actual problem: with the screen off, the sweep is
 * JavaScript that is not running. Every other background feature in this app is native (push, the
 * media session, the signer) and every other sync app on Android transfers in a foreground service.
 * This is that.
 *
 * WHAT IT DELIBERATELY DOES NOT DO, and this is the design rather than a shortfall. The background
 * sweep MOVES BYTES; anything that needs a decision waits for a foreground sweep, where the whole
 * executor and a person are both available:
 *
 *   * CONFLICTS are deferred. Settling one properly means hashing, comparing chunk lists and
 *     sometimes renaming somebody's file; leaving the path unagreed costs nothing, because the next
 *     sweep simply proposes it again.
 *   * a mass DELETE or a mass RESURRECT is refused, never asked. That is the standing rule for a
 *     sweep nobody is watching, and refusing suppresses only the deletions (or only the
 *     resurrections) — a guard that aborts the whole sweep is the same bug with the sign flipped,
 *     which is exactly what happened to the contacts sweep.
 *
 * Everything it DOES do goes through the same engine the browser uses: {@link SyncDiff} is a port of
 * foldersync.js held to the same answers by a differential test, so a phone and a laptop cannot
 * decide differently about the same three snapshots.
 */
public final class NativeSweep {

    private NativeSweep() { }

    /** Past this a file goes up in pieces — the same figure syncrun.js uses as `chunkAbove`. */
    static final long CHUNK_ABOVE = 16L * 1024 * 1024;
    /**
     * One chunk. 4 MB, matching fs-android.js — chosen there because every chunk crossed the
     * Capacitor bridge as base64. Nothing crosses a bridge here, but the SIZE IS PART OF THE FILE'S
     * IDENTITY: `same()` compares chunk lists only at equal `cs`, so a native sweep that picked its
     * own number would make every chunked file this phone uploaded look different from the same file
     * uploaded by the same phone through the page.
     */
    static final int CHUNK_BYTES = 4 * 1024 * 1024;
    /** Past this the manifest's paths move into an encrypted blob — sync.js MANIFEST_INLINE_MAX. */
    static final int MANIFEST_INLINE_MAX = 45000;
    static final int CHECKPOINT = 200;
    static final int MAX_CHECKPOINTS = 20;

    /** What one sweep did, in the shape the panel reads. */
    public static final class Report {
        public final List<String> uploaded = new ArrayList<String>();
        public final List<String> downloaded = new ArrayList<String>();
        public final List<String> trashed = new ArrayList<String>();
        public final List<String> removedRemote = new ArrayList<String>();
        public final List<Map<String, Object>> failed = new ArrayList<Map<String, Object>>();
        public int unchanged, excluded, deferred, alreadyStored, checkpoints, repaired;
        public boolean hashed = false;
        public String refusedTrash = "", refusedResurrect = "", error = "", deferredWhy = "";
        public long at = System.currentTimeMillis();
        public String key = "";

        public Map<String, Object> toMap() {
            Map<String, Object> m = new LinkedHashMap<String, Object>();
            m.put("key", key);
            m.put("at", at);
            m.put("uploaded", uploaded.size());
            m.put("downloaded", downloaded.size());
            m.put("trashed", trashed.size());
            m.put("removedRemote", removedRemote.size());
            m.put("failed", failed.size());
            m.put("unchanged", (long) unchanged);
            m.put("deferred", (long) deferred);
            m.put("alreadyStored", (long) alreadyStored);
            m.put("checkpoints", (long) checkpoints);
            m.put("hashed", hashed);
            if (repaired > 0) m.put("repaired", (long) repaired);
            if (!refusedTrash.isEmpty()) m.put("refusedTrash", refusedTrash);
            if (!refusedResurrect.isEmpty()) m.put("refusedResurrect", refusedResurrect);
            if (!deferredWhy.isEmpty()) m.put("deferredWhy", deferredWhy);
            if (!error.isEmpty()) m.put("error", error);
            if (!failed.isEmpty()) m.put("firstFailure", failed.get(0));
            return m;
        }
    }

    /** Cancelled between files — a transfer in flight finishes, because interrupting wastes it. */
    public interface Stop { boolean stopping(); }

    /**
     * ONE SWEEP AT A TIME, ACROSS BOTH ENGINES. The page can start a sweep of the same folder at any
     * moment (the user opens the app mid-alarm), and two sweeps writing the same manifest is
     * last-writer-wins on a document that decides whether files exist. `FolderSyncPlugin.sweepBegin`
     * takes this too, so the JavaScript path and this one cannot overlap.
     */
    private static final Set<String> BUSY = new LinkedHashSet<String>();

    public static synchronized boolean claim(String key) { return BUSY.add(key); }

    public static synchronized void release(String key) { BUSY.remove(key); }

    /**
     * Everything the PAGE is holding, dropped — called when the page goes away.
     *
     * A claim taken across the bridge is released in the sweep's `finally`, which does not run when
     * the renderer is killed mid-sweep or the activity is destroyed while backgrounded. That is not
     * an edge case here: it is precisely the situation this whole native path exists for. Without
     * this the pair key stays claimed for the life of the PROCESS, and afterwards neither engine can
     * touch that folder ever again — the native sweep answers "already syncing" and the reloaded
     * page is told "this folder is syncing in the background", on every press, until the app is
     * force-stopped.
     *
     * Only the page's own claims: a native sweep genuinely running in the service must survive the
     * page dying, which is the entire point of it.
     */
    public static synchronized void releaseAll(Set<String> keys) { BUSY.removeAll(keys); }

    // -------------------------------------------------------------------------------- the sweep

    public static Report run(Context ctx, SyncStore store, SyncStore.Folder f, byte[] sec,
                             boolean hash, Stop stop) {
        Report rep = new Report();
        rep.key = f.key;
        rep.hashed = hash;
        if (!claim(f.key)) {
            rep.deferred = 1;
            rep.deferredWhy = "already syncing";
            return rep;
        }
        try {
            sweep(ctx, store, f, sec, hash, stop, rep);
        } catch (Throwable t) {
            rep.error = String.valueOf(t.getMessage() == null ? t : t.getMessage());
        } finally {
            release(f.key);
        }
        return rep;
    }

    private static void sweep(Context ctx, SyncStore store, SyncStore.Folder f, byte[] sec,
                              boolean hash, Stop stop, Report rep) throws Exception {
        final long now = System.currentTimeMillis();
        final String device = store.deviceName();
        SyncNet net = new SyncNet(store.apiBase(), store.mediaBase(), sec);
        byte[] mk = SyncCrypto.unwrapMasterKey(sec, store.wrappedDriveKey());
        SafFs fs = new SafFs(ctx, f.id);

        Map<String, Map<String, Object>> remote = readManifest(net, sec, mk, f.key);
        Map<String, Map<String, Object>> base = store.base(f.key);

        /* AN EMPTY AGREEMENT FORCES A HASH, IT DOES NOT STOP THE SWEEP — and the first version of
         * this got that exactly backwards.
         *
         * It deferred instead, on the reasoning that a first sweep is expensive and dangerous. The
         * reasoning was fine and the consequence was that the whole native path could never run
         * ONCE, on any device: `base` here is written only by this sweep, the page keeps its own copy
         * in IndexedDB where Java cannot reach it, so the agreement was empty for ever and every
         * alarm answered "first sync — open the app once". Opening the app did nothing, because the
         * page writes only its own copy. Dead on arrival, and reported as success.
         *
         * What the deferral was actually protecting against is covered elsewhere: with no `base` both
         * sides look changed for every path at once, so the answer has to come from CONTENT — that is
         * the hash — and the thing that would go wrong without it is a conflict copy of every file.
         * Conflicts are deferred by this sweep regardless (see the class comment), the mass-delete and
         * mass-resurrect guards refuse in bulk, and a folder nobody has pressed Start on is `paused`,
         * which `shouldSync` declines before we ever get here. So the honest move is to pay for the
         * hash and settle the folder, which is what the browser's first sweep does too.
         *
         * OTHERWISE hashing is the charging-time job the caller already decided about: `shouldSync`
         * answers `full` when plugged in and it has been a day. Rehashing on every sweep is the space
         * heater the battery policy exists to avoid; never rehashing means an entry with no content
         * identity never gains one, and every device that joins later falls back to size+mtime —
         * which on Android can never match, because SAF assigns its own last-modified. */
        boolean firstEver = base.isEmpty();
        if (firstEver) { hash = true; rep.hashed = true; }
        SafFs.Scan scan = fs.scan(hash, 0, f.excludes);
        Map<String, Map<String, Object>> local = new LinkedHashMap<String, Map<String, Object>>();
        for (Map.Entry<String, Map<String, Object>> e : scan.files.entrySet()) {
            /* The scan reports the FILE's hash in `sha`; a manifest entry's `sha` is the address of
             * its encrypted blob. Renaming it to `csum` here is what stops the engine ever comparing
             * the two — which it used to, and called every identical file divergent. */
            Map<String, Object> src = e.getValue();
            Map<String, Object> out = new LinkedHashMap<String, Object>();
            out.put("size", src.get("size"));
            out.put("mtime", src.get("mtime"));
            Object sha = src.get("sha");
            if (sha instanceof String && !((String) sha).isEmpty()) out.put("csum", sha);
            local.put(e.getKey(), out);
        }

        final Map<String, Map<String, Object>> nextRemote0 =
                new LinkedHashMap<String, Map<String, Object>>(remote);
        final Set<String> touched0 = new LinkedHashSet<String>();
        SyncDiff.Plan plan = SyncDiff.diff(local, remote, base, f.excludes, device, now);
        rep.unchanged = plan.unchanged;
        rep.excluded = plan.excluded;
        rep.deferred += plan.conflicts.size();
        if (!plan.conflicts.isEmpty()) {
            rep.deferredWhy = plan.conflicts.size() + " conflict"
                    + (plan.conflicts.size() == 1 ? "" : "s") + " need the app open";
        }

        /* NOBODY IS WATCHING, SO A REFUSAL IS THE ANSWER — and it suppresses one bucket, never the
         * sweep. The refused paths are deliberately NOT agreed, so the next sweep proposes exactly
         * this again and a foreground one can ask. */
        Map<String, Object> mass = SyncDiff.massDelete(plan);
        List<Map<String, Object>> deleteLocal = plan.deleteLocal;
        if (mass != null) {
            rep.refusedTrash = Json.str(mass.get("why"), "refused");
            deleteLocal = new ArrayList<Map<String, Object>>();
        }
        List<Map<String, Object>> uploads = plan.upload;
        Map<String, Object> massUp = SyncDiff.massResurrect(plan);
        if (massUp != null) {
            rep.refusedResurrect = Json.str(massUp.get("why"), "refused");
            uploads = new ArrayList<Map<String, Object>>();
            for (Map<String, Object> u : plan.upload) {
                if (!Json.bool(u.get("resurrect"), false)) uploads.add(u);
            }
        }

        final Map<String, Map<String, Object>> nextRemote = nextRemote0;
        final Map<String, Map<String, Object>> nextBase =
                new LinkedHashMap<String, Map<String, Object>>(base);
        final Set<String> touched = touched0;

        /* GIVE EVERY ENTRY A CONTENT IDENTITY WHILE WE ARE HERE.
         *
         * An entry written before `csum` existed can never be compared by content, so every device
         * that hashes falls back to size+mtime — which Android can never match, because SAF assigns
         * its own last-modified. Those paths conflict for ever, and a conflict is exactly what this
         * sweep DEFERS, so without this a folder full of them would be re-swept every sixteen
         * minutes and settle nothing, on every phone, permanently.
         *
         * It is not a new fact — it is the one this sweep just established, written down where the
         * other devices can use it. `same()` had to be true to get here, so a path that is genuinely
         * different is untouched. */
        int repaired = 0;
        for (Map.Entry<String, Map<String, Object>> e : local.entrySet()) {
            Map<String, Object> L = e.getValue(), R = remote.get(e.getKey());
            if (L == null || Json.str(L.get("csum"), "").isEmpty()) continue;
            if (R == null || R.get("csum") != null || SyncDiff.gone(R)) continue;
            if (!SyncDiff.same(L, R)) continue;
            Map<String, Object> up = new LinkedHashMap<String, Object>(R);
            up.put("csum", L.get("csum"));
            nextRemote0.put(e.getKey(), up);
            touched0.add(e.getKey());
            repaired++;
        }
        rep.repaired = repaired;

        int work = deleteLocal.size() + plan.download.size() + uploads.size();
        int every = Math.max(CHECKPOINT, (int) Math.ceil(work / (double) MAX_CHECKPOINTS));
        Check check = new Check(net, store, sec, mk, f.key, nextRemote, nextBase, touched, every, rep);
        if (repaired > 0) check.markDirty();

        /* DELETIONS FIRST, BEFORE ANY BYTE MOVES. Queued behind hours of transfer they are simply
         * never reached: a sweep is interrupted, restarts its transfer loops from the top, and the
         * deletions sit there across sweep after sweep. A local delete is a rename into .pc-trash —
         * instant, no network — so there is no reason for it to wait behind a 40 GB upload. It does
         * not break "download before delete": diff() puts each path in exactly one bucket. */
        for (Map<String, Object> t : deleteLocal) {
            if (stop != null && stop.stopping()) { check.flush(); return; }
            String path = Json.str(t.get("path"), "");
            try {
                fs.trash(path, now);
                Map<String, Object> tomb = new LinkedHashMap<String, Object>();
                Map<String, Object> R = remote.get(path);
                tomb.put("deletedAt", R == null ? now : Json.num(R.get("deletedAt"), now));
                check.agree(path, tomb);
                rep.trashed.add(path);
            } catch (Exception e) { fail(rep, path, "delete", e); }
            check.maybe();
        }

        for (Map<String, Object> d : plan.download) {
            if (stop != null && stop.stopping()) { check.flush(); return; }
            String path = Json.str(d.get("path"), "");
            Map<String, Object> R = remote.get(path);
            if (R == null) continue;
            try {
                long[] st = download(net, fs, mk, path, R, now);
                Map<String, Object> agreed = new LinkedHashMap<String, Object>();
                // Absent, never null: a chunked entry has no `sha` at all — its address is the LIST —
                // and writing an explicit null would be a value the next sweep has to reason about.
                if (R.get("sha") != null) agreed.put("sha", R.get("sha"));
                if (R.get("csum") != null) agreed.put("csum", R.get("csum"));
                /* `cs` TRAVELS WITH `chunks`, always. An entry is comparable only to one made at the
                 * same chunk size, so an agreement that kept the list and dropped the size can never
                 * match the manifest entry it came from — and the file is re-downloaded on every
                 * sweep for ever, each commit moving the previous copy into .pc-trash. */
                if (R.get("chunks") != null) {
                    agreed.put("chunks", R.get("chunks"));
                    if (R.get("cs") != null) agreed.put("cs", R.get("cs"));
                }
                agreed.put("size", st[0]);
                agreed.put("mtime", st[1]);
                check.agree(path, agreed);
                rep.downloaded.add(path);
            } catch (Exception e) { fail(rep, path, "download", e); }
            check.maybe();
        }

        for (Map<String, Object> u : uploads) {
            if (stop != null && stop.stopping()) { check.flush(); return; }
            String path = Json.str(u.get("path"), "");
            Map<String, Object> meta = local.get(path);
            if (meta == null) continue;
            try {
                Map<String, Object> entry = upload(net, fs, mk, path, meta, device, now, rep);
                check.remember(path, entry);
                check.agree(path, entry);
                rep.uploaded.add(path);
            } catch (Exception e) { fail(rep, path, "upload", e); }
            check.maybe();
        }

        for (Map<String, Object> r : plan.deleteRemote) {
            String path = Json.str(r.get("path"), "");
            Map<String, Object> tomb = new LinkedHashMap<String, Object>();
            tomb.put("deletedAt", now);
            check.remember(path, tomb);              // a tombstone, so other devices learn of it
            check.agree(path, tomb);
            rep.removedRemote.add(path);
        }

        // Paths the engine settled with no I/O still have to be recorded, or every sweep re-decides
        // them for ever. (`deleted on both`, `same content both sides`.)
        for (Map<String, Object> n : plan.notes) {
            String path = Json.str(n.get("path"), "");
            Map<String, Object> L = local.get(path), R = remote.get(path);
            Map<String, Object> agreed = new LinkedHashMap<String, Object>();
            if (L != null) {
                if (L.get("csum") != null) agreed.put("csum", L.get("csum"));
                if (R != null && R.get("chunks") != null) agreed.put("chunks", R.get("chunks"));
                agreed.put("size", L.get("size"));
                agreed.put("mtime", L.get("mtime"));
            } else {
                agreed.put("deletedAt", R == null ? now : Json.num(R.get("deletedAt"), now));
            }
            check.agree(path, agreed);
        }

        check.flush();
    }

    private static void fail(Report rep, String path, String what, Exception e) {
        Map<String, Object> m = new LinkedHashMap<String, Object>();
        m.put("path", path);
        m.put("what", what);
        m.put("error", e.getMessage() == null ? String.valueOf(e) : e.getMessage());
        rep.failed.add(m);
    }

    // ------------------------------------------------------------------------------- transfers

    /**
     * One file down, whole or in pieces, into the `.part` file — never under the real name until it
     * has been checked. A checksum mismatch throws BEFORE the commit, so what is on disk is left
     * alone and the part file is discarded rather than trashed: those are bytes we could not
     * confirm, and putting them in the safety net makes the net less trustworthy.
     */
    private static long[] download(SyncNet net, SafFs fs, byte[] mk, String path,
                                   Map<String, Object> R, long now) throws Exception {
        List<Object> chunks = R.get("chunks") instanceof List ? Json.arr(R.get("chunks")) : null;
        long mtime = Json.num(R.get("mtime"), 0);
        String csum = Json.str(R.get("csum"), "");
        if (chunks != null && !chunks.isEmpty()) {
            long expect = Json.num(R.get("size"), -1);
            long off = 0;
            /* RESUME ONLY WHERE THE RESULT CAN BE CHECKED. A part file is tied to a chunk list by
             * nothing but its length, so resuming onto one left by an EARLIER generation of this
             * path splices two files together and only the checksum catches it. */
            int skip = 0;
            long cs = Json.num(R.get("cs"), 0);
            if (!csum.isEmpty() && cs > 0) {
                long have = fs.partSize(path);
                if (have > 0 && have % cs == 0) {
                    long whole = have / cs;
                    if (whole < chunks.size()) { skip = (int) whole; off = have; }
                }
            } else {
                fs.discardPart(path);   // or it is resumed onto the moment a csum appears
            }
            for (int i = skip; i < chunks.size(); i++) {
                byte[] plain = SyncCrypto.decrypt(mk, net.getBlob(String.valueOf(chunks.get(i))));
                fs.writePart(path, off, plain);
                off += plain.length;
            }
            /* ONE SHORT OR WRONG-SIZED CHUNK SHIFTS EVERY BYTE AFTER IT, and the file lands the right
             * shape with the wrong content. The manifest's size is the one check that catches a bad
             * chunk list — including lists already stored by an older uploader.
             *
             * THE PART FILE IS DISCARDED HERE, which the browser does not do — it throws from inside
             * getParts, before the caller's discard can run. That leaves a part file whose length may
             * still be a whole multiple of the chunk size, so the NEXT attempt resumes onto the same
             * bad prefix and fails identically, for ever. Nothing is at risk in throwing it away: it
             * is not the user's file, it is bytes we have just proved wrong, and the cost of being
             * cautious is one full download. */
            if (expect >= 0 && off != expect) {
                fs.discardPart(path);
                throw new java.io.IOException("rebuilt " + off + " bytes, the manifest says " + expect);
            }
        } else {
            String sha = Json.str(R.get("sha"), "");
            if (sha.isEmpty()) throw new java.io.IOException("the manifest entry names no blob");
            byte[] plain = SyncCrypto.decrypt(mk, net.getBlob(sha));
            fs.writePart(path, 0, plain);
        }
        if (!csum.isEmpty()) {
            String got = fs.hashPart(path);
            if (got != null && !got.equals(csum)) {
                fs.discardPart(path);
                throw new java.io.IOException("checksum mismatch after download — refusing to write it");
            }
        }
        return fs.commitPart(path, mtime);
    }

    /** One file up, whole or in pieces, and the manifest entry it earns. */
    private static Map<String, Object> upload(SyncNet net, SafFs fs, byte[] mk, String path,
                                              Map<String, Object> meta, String device, long now,
                                              Report rep) throws Exception {
        long size = Json.num(meta.get("size"), 0);
        long mtime = Json.num(meta.get("mtime"), now);
        Map<String, Object> entry = new LinkedHashMap<String, Object>();
        if (size > CHUNK_ABOVE) {
            List<Object> chunks = new ArrayList<Object>();
            boolean allExisted = true;
            for (long off = 0; off < size; off += CHUNK_BYTES) {
                int want = (int) Math.min(CHUNK_BYTES, size - off);
                /* A CHUNK IS EXACTLY THE BYTES THAT WERE ASKED FOR, OR IT IS A FAILURE. A short read
                 * that is encrypted, uploaded and recorded while `off` advances by a whole chunk
                 * leaves the bytes in the gap stored by nobody — the file that comes back down is the
                 * original with holes punched in it, and only large files are chunked, so it could
                 * never touch a photo and could never miss a video. One retry for a provider hiccup;
                 * after that the file fails and is reported, which is recoverable. */
                byte[] plain = fs.readRange(path, off, want);
                if (plain.length != want) plain = fs.readRange(path, off, want);
                if (plain.length != want) {
                    throw new java.io.IOException("short read at " + off + ": wanted " + want
                                                  + ", got " + plain.length);
                }
                byte[] blob = SyncCrypto.encrypt(mk, plain);
                String sha = SyncCrypto.sha256hex(blob);
                if (net.blobExists(sha)) { chunks.add(sha); }
                else { chunks.add(net.putBlob(blob)); allExisted = false; }
            }
            if (allExisted) rep.alreadyStored++;
            entry.put("chunks", chunks);
            entry.put("cs", (long) CHUNK_BYTES);
            /* Whatever content identity the scan established travels with it. `chunks` IS an identity
             * on its own, but only at this chunk size — a desktop splitting the same file 16 MB at a
             * time produces a list with nothing in common — so a `csum` is what lets the two agree. It
             * is only here when this was a rehashing sweep; an incremental one leaves it out, exactly
             * as the browser does. */
            String big = Json.str(meta.get("csum"), "");
            if (!big.isEmpty()) entry.put("csum", big);
        } else {
            byte[] plain = fs.readAll(path);
            byte[] blob = SyncCrypto.encrypt(mk, plain);
            String sha = SyncCrypto.sha256hex(blob);
            if (net.blobExists(sha)) rep.alreadyStored++;
            else net.putBlob(blob);
            entry.put("sha", sha);
            /* The csum is computed here when the scan did not hash, because it is the only thing that
             * lets ANOTHER device recognise this file as one it already has. Without it the manifest
             * carries no content identity and every joining device falls back to size+mtime — which
             * on Android cannot match, since SAF assigns its own last-modified. We are holding the
             * bytes already; the hash is the cheapest part of this loop. */
            String csum = Json.str(meta.get("csum"), "");
            entry.put("csum", csum.isEmpty() ? SyncCrypto.sha256hex(plain) : csum);
        }
        entry.put("size", size);
        entry.put("mtime", mtime);
        entry.put("device", device);
        return entry;
    }

    // -------------------------------------------------------------------------- the manifest

    /** The shared agreement, decrypted — v2 pointer blob, sealed inline, or a pre-seal document. */
    static Map<String, Map<String, Object>> readManifest(SyncNet net, byte[] sec, byte[] mk, String key)
            throws Exception {
        Map<String, Object> doc = Json.obj(net.manifest(key, null, false).get("manifest"));
        String pathsSha = Json.str(doc.get("pathsSha"), "");
        String json;
        if (!pathsSha.isEmpty()) {
            json = SyncCrypto.fromUtf8(SyncCrypto.decrypt(mk, net.getBlob(pathsSha)));
        } else {
            String sealed = Json.str(doc.get("sealed"), "");
            if (sealed.isEmpty()) {
                Map<String, Map<String, Object>> out = new LinkedHashMap<String, Map<String, Object>>();
                for (Map.Entry<String, Object> e : Json.obj(doc.get("paths")).entrySet()) {
                    out.put(e.getKey(), Json.obj(e.getValue()));
                }
                return out;                                   // pre-seal manifests, still readable
            }
            /* THE `v2:` MARKER IS NOT DECORATION. A document whose paths live in a blob still sets
             * `sealed`, to something that cannot possibly decrypt, so a client that does not
             * understand the pointer THROWS rather than reading the manifest as EMPTY — which is not
             * a harmless misread: an empty remote means every file is "deleted elsewhere", and that
             * device would trash all of them and publish tombstones the others would honour. */
            json = SyncCrypto.openFromSelf(sec, sealed);
        }
        Map<String, Map<String, Object>> out = new LinkedHashMap<String, Map<String, Object>>();
        for (Map.Entry<String, Object> e : Json.obj(Json.parse(json)).entrySet()) {
            out.put(e.getKey(), Json.obj(e.getValue()));
        }
        return out;
    }

    /**
     * Store the agreement, then the local one. Never the other way round: a `base` that runs ahead of
     * the manifest makes this device believe in an agreement the others never saw.
     */
    static void saveManifest(SyncNet net, SyncStore store, byte[] sec, byte[] mk, String key,
                             Map<String, Map<String, Object>> paths,
                             Map<String, Map<String, Object>> base,
                             Set<String> touched, int removed) throws Exception {
        /* MERGE, DO NOT OVERWRITE. The manifest is one replaceable document and our copy is a
         * snapshot from when this sweep started, so writing it whole means the later of two devices
         * erases every path the other added — silently, because the blobs are still there and only
         * the entries are gone. A read that FAILS falls back to our snapshot, which is worse than
         * merging and better than not saving at all; the server's collapse guard still stands behind
         * it, because a merge can only ever add. */
        Map<String, Map<String, Object>> out = paths;
        try {
            Map<String, Map<String, Object>> fresh = readManifest(net, sec, mk, key);
            for (String p : touched) if (paths.containsKey(p)) fresh.put(p, paths.get(p));
            out = fresh;
        } catch (Exception ignored) { }

        int live = 0;
        for (Map<String, Object> e : out.values()) if (SyncDiff.live(e)) live++;
        String json = Json.write(out);
        Map<String, Object> doc = new LinkedHashMap<String, Object>();
        doc.put("n", (long) live);
        if (json.length() < MANIFEST_INLINE_MAX) {
            doc.put("sealed", SyncCrypto.sealToSelf(sec, json));
        } else {
            /* NIP-44 refuses a plaintext over 65535 bytes and an entry is ~174 of them, so this
             * document could hold about 376 files and a bigger folder COULD NOT BE SAVED AT ALL: the
             * blobs all uploaded, the save threw at the very last step, `base` was never written, and
             * the next sweep started from the beginning. For ever. */
            String sha = net.putBlob(SyncCrypto.encrypt(mk, SyncCrypto.utf8(json)));
            doc.put("pathsSha", sha);
            doc.put("sealed", "v2:" + sha);               // the marker above — deliberately undecryptable
        }
        try {
            net.manifest(key, doc, false);
        } catch (SyncNet.Collapse c) {
            /* THE SERVER REFUSED A SHRINK. It holds a count and nothing else, so a deliberate mass
             * delete and a broken client about to empty the folder look identical from there. THIS
             * side is not guessing: if the deletions this sweep made account for the shrink, the
             * write is precisely what was asked for. If they do not, a background sweep has nobody to
             * ask — so it refuses, and the agreement is not written, and the next foreground sweep
             * proposes the same thing to somebody who can answer. */
            if (removed > 0 && removed >= c.shrink()) net.manifest(key, doc, true);
            else throw new java.io.IOException("the server refused a write that shrinks “" + key
                                               + "” from " + c.oldCount + " to " + c.newCount
                                               + " — open the app to confirm it");
        }
        store.saveBase(key, base);
    }

    /** Checkpointing, so an interrupted sweep resumes instead of starting from file one. */
    private static final class Check {
        private final SyncNet net;
        private final SyncStore store;
        private final byte[] sec, mk;
        private final String key;
        private final Map<String, Map<String, Object>> remote, base;
        private final Set<String> touched;
        private final int every;
        private final Report rep;
        private boolean dirty = false;
        private int since = 0;

        Check(SyncNet net, SyncStore store, byte[] sec, byte[] mk, String key,
              Map<String, Map<String, Object>> remote, Map<String, Map<String, Object>> base,
              Set<String> touched, int every, Report rep) {
            this.net = net; this.store = store; this.sec = sec; this.mk = mk; this.key = key;
            this.remote = remote; this.base = base; this.touched = touched; this.every = every;
            this.rep = rep;
        }

        /** A repair changed the manifest without agreeing anything, and it still has to be stored. */
        void markDirty() { dirty = true; }

        void remember(String path, Map<String, Object> entry) {
            remote.put(path, entry);
            touched.add(path);
        }

        void agree(String path, Map<String, Object> entry) {
            base.put(path, entry);
            dirty = true;
        }

        /** A FAILED CHECKPOINT IS NOT A FAILED SWEEP — the work is real either way. */
        void maybe() {
            if (!dirty || ++since < every) return;
            since = 0;
            try {
                saveManifest(net, store, sec, mk, key, remote, base, touched, rep.removedRemote.size());
                rep.checkpoints++;
            } catch (Exception e) {
                rep.error = "checkpoint: " + e.getMessage();
            }
        }

        /** The final save is deliberately NOT a checkpoint: this one failing means the sweep's whole
         *  result was never recorded, so it throws. */
        void flush() throws Exception {
            if (!dirty) return;
            saveManifest(net, store, sec, mk, key, remote, base, touched, rep.removedRemote.size());
        }
    }
}
