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

    /**
     * One chunk. 4 MB, matching fs-android.js — chosen there because every chunk crossed the
     * Capacitor bridge as base64. Nothing crosses a bridge here, but the SIZE IS PART OF THE FILE'S
     * IDENTITY: `same()` compares chunk lists only at equal `cs`, so a native sweep that picked its
     * own number would make every chunked file this phone uploaded look different from the same file
     * uploaded by the same phone through the page.
     */
    static final int CHUNK_BYTES = 4 * 1024 * 1024;
    /**
     * Past this a file goes up in pieces — and it is ONE CHUNK, not sixteen megabytes.
     *
     * It used to be 16 MB, borrowed from the desktop, which meant a 12 MB video went up whole on the
     * NATIVE path while the page chunked the same file at 4 MB. Two costs, both real: a whole-file
     * upload holds the plaintext, the ciphertext and the request body at once — some 36 MB of Java
     * heap for that video, on a device whose heap is a couple of hundred megabytes and which is also
     * running a WebView — and the two engines produced different shapes for the same file.
     *
     * A file bigger than one chunk goes in chunks. That is the whole rule, on both engines.
     */
    static final long CHUNK_ABOVE = CHUNK_BYTES;
    static final int CHECKPOINT = 200;
    static final int MAX_CHECKPOINTS = 20;

    /** What one sweep did, in the shape the panel reads. */
    public static final class Report {
        /** Distinct devices named by the record set. There are no per-device views any more, so
         *  nothing can be partially read: a record set that could not be fetched throws and the
         *  sweep changes nothing. */
        public int devices = 0;
        /** Set when this sweep refused to tell the other devices to delete in bulk. */
        public String refusedRemoteDelete = null;
        public final List<String> uploaded = new ArrayList<String>();
        public int settledByContent = 0;
        public int heldAlready = 0;
        public final List<String> downloaded = new ArrayList<String>();
        public final List<String> trashed = new ArrayList<String>();
        public final List<String> removedRemote = new ArrayList<String>();
        /** Deletion claims held back for lack of positive proof — the card says how many and why. */
        public final List<String> unconfirmedAbsent = new ArrayList<String>();
        /* THE REPAIR OF A FLAGGED RECORD, in three answers. `reseeding` is what went back up;
         * `staleChecksum` is the subset where the bytes were fine and the published checksum was
         * the thing that was wrong; `badHere` is where this device's own copy disagreed with both
         * and nothing was sent. Reported rather than counted, because "which files" is the only
         * useful form of this. */
        public final List<String> reseeding = new ArrayList<String>();
        public final List<String> staleChecksum = new ArrayList<String>();
        public final List<String> badHere = new ArrayList<String>();
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
            m.put("settledByContent", settledByContent);
            m.put("heldAlready", heldAlready);
            m.put("downloaded", downloaded.size());
            m.put("trashed", trashed.size());
            m.put("removedRemote", removedRemote.size());
            m.put("unconfirmedAbsent", unconfirmedAbsent.size());
            if (!reseeding.isEmpty()) m.put("reseeding", (long) reseeding.size());
            if (!staleChecksum.isEmpty()) m.put("staleChecksum", (long) staleChecksum.size());
            if (!badHere.isEmpty()) m.put("badHere", (long) badHere.size());
            m.put("failed", failed.size());
            m.put("unchanged", (long) unchanged);
            m.put("deferred", (long) deferred);
            m.put("alreadyStored", (long) alreadyStored);
            m.put("checkpoints", (long) checkpoints);
            m.put("hashed", hashed);
            if (repaired > 0) m.put("repaired", (long) repaired);
            if (!refusedTrash.isEmpty()) m.put("refusedTrash", refusedTrash);
            if (!refusedResurrect.isEmpty()) m.put("refusedResurrect", refusedResurrect);
            /* The outward-pointing refusal was the one kind the report did not carry, so a phone
             * that declined to tell every other device to delete said so nowhere a person could
             * read it. All three refusals need a person; all three now travel. */
            if (refusedRemoteDelete != null) m.put("refusedRemoteDelete", refusedRemoteDelete);
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
    /** When each claim was taken — see {@link #claimed}. */
    private static final java.util.Map<String, Long> CLAIMED_AT = new java.util.HashMap<String, Long>();

    /**
     * A CLAIM MAY BLOCK A SWEEP; IT MAY NOT BLOCK THE CLOCK FOR EVER.
     *
     * `claimed()` is only an optimisation — it stops the alarm starting a foreground service for a
     * folder the page is already sweeping. But a claim can be STUCK: the page's sweep is JavaScript
     * in a WebView, and a WebView can hang, be throttled to a standstill, or be killed between
     * taking the claim and the `finally` that returns it. (handleOnDestroy covers the kill; it does
     * not cover a page that is alive and wedged.) Treated as authoritative, one such claim answers
     * "nothing is due" on every tick from then on, and background sync is silently dead for the life
     * of the process — a hung foreground sync and a background sync that never runs being, in that
     * case, the same bug reported twice.
     *
     * So a claim goes STALE. Past the bound this reports the folder free and the alarm proceeds;
     * `claim()` itself is unchanged and still refuses, so the two engines can never actually overlap
     * — the worst case is one wasted wake-up, which is exactly the cost this method exists to avoid
     * and enormously cheaper than the failure it was causing.
     */
    private static final long CLAIM_STALE_MS = 20 * 60 * 1000L;

    /**
     * @return false only when something is ACTIVELY holding this folder. A claim older than the
     *         stale bound is STOLEN, and that is the difference between a wedged sweep costing one
     *         cycle and it bricking the folder.
     *
     * Reported as *"pictures is set to already syncing and stuck / no progress"*: the page asks for
     * the claim on every attempt, is refused, and shows "syncing in the background — it will finish
     * on its own". If the holder is never coming back, that is the card's permanent state and the
     * only way out is force-stopping the app. Nothing in the sweep can promise to return a claim: it
     * runs on a thread that can be frozen with the process, killed with the renderer, or blocked on a
     * socket the platform never times out. So the claim carries an expiry rather than a promise.
     */
    public static synchronized boolean claim(String key) {
        long now = System.currentTimeMillis();
        if (BUSY.contains(key)) {
            Long at = CLAIMED_AT.get(key);
            if (at != null && now - at >= CLAIM_STALE_MS) {
                // Steal it. The previous holder is gone or wedged; if it ever does come back its
                // `finally` releases a claim it no longer owns, which is harmless — the worst case
                // is one overlapping sweep, and `store.save`'s re-read-and-merge is what makes that
                // survivable. A folder nobody can ever sync again is not.
                CLAIMED_AT.put(key, now);
                return true;
            }
            return false;
        }
        BUSY.add(key);
        CLAIMED_AT.put(key, now);
        return true;
    }

    /* LIVE PROGRESS OF THE SWEEP THAT IS RUNNING NOW, so the page has something true to show while
     * it is locked out. The card used to print one static sentence when its claim was refused, which
     * on a folder of any size is indistinguishable from a hang — and was reported as exactly that.
     * These are written on the sweep thread and read from the plugin; ints, so no lock is needed for
     * a number that is only ever displayed. */
    private static volatile String liveKey = "", livePhase = "", livePath = "";
    private static volatile int liveDone = 0, liveTotal = 0;
    private static volatile long liveAt = 0;

    static void progress(String key, String phase, String path, int done, int total) {
        liveKey = key == null ? "" : key;
        livePhase = phase == null ? "" : phase;
        livePath = path == null ? "" : path;
        liveDone = done;
        liveTotal = total;
        liveAt = System.currentTimeMillis();
    }

    /** Nothing is running any more — so the page stops drawing a line that has stopped moving. */
    static void progressDone(String key) {
        if (liveKey.equals(key == null ? "" : key)) { liveKey = ""; livePhase = ""; livePath = ""; }
    }

    /** {key, phase, path, done, total, at} for whatever is sweeping, or null when nothing is. */
    public static synchronized Map<String, Object> live() {
        if (liveKey.isEmpty() || !BUSY.contains(liveKey)) return null;
        Map<String, Object> m = new LinkedHashMap<String, Object>();
        m.put("key", liveKey);
        m.put("phase", livePhase);
        m.put("path", livePath);
        m.put("done", (long) liveDone);
        m.put("total", (long) liveTotal);
        m.put("at", liveAt);
        return m;
    }

    /** Whether something already holds this folder, and holds it RECENTLY. ASKED, not taken —
     *  {@link NativeRunner#plan} uses it to decide there is nothing to wake the phone for, and taking
     *  the claim there would mean the caller has to give it back on every path that declines. */
    public static synchronized boolean claimed(String key) {
        if (!BUSY.contains(key)) return false;
        Long at = CLAIMED_AT.get(key);
        if (at == null) return true;
        return System.currentTimeMillis() - at < CLAIM_STALE_MS;
    }

    public static synchronized void release(String key) { BUSY.remove(key); CLAIMED_AT.remove(key); }

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
    public static synchronized void releaseAll(Set<String> keys) {
        BUSY.removeAll(keys);
        CLAIMED_AT.keySet().removeAll(keys);   // or the timestamps outlive the claims
    }

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
            SyncNet net = new SyncNet(store.apiBase(), store.mediaBase(), sec);
            byte[] mk = SyncCrypto.unwrapMasterKey(sec, store.wrappedDriveKey());
            sweep(ctx, store, f, sec, hash, stop, rep, net, mk, new SafFs(ctx, f.id));
        } catch (Throwable t) {
            rep.error = String.valueOf(t.getMessage() == null ? t : t.getMessage());
        } finally {
            // Before the claim goes: the page reads `live()` only while the folder is claimed, and a
            // line left behind by a finished sweep is exactly the stale progress this exists to end.
            progressDone(f.key);
            release(f.key);
        }
        return rep;
    }

    /** Package-private and taking its io: that is what makes a whole sweep runnable off a phone.
     *  See SyncIo — the disk and the network used to be built in here, so this could be compiled and
     *  read and never executed. */
    static void sweep(Context ctx, SyncStore store, SyncStore.Folder f, byte[] sec,
                      boolean hash, Stop stop, Report rep,
                      SyncIo.Net net, byte[] mk, SyncIo.Files fs) throws Exception {
        final long now = System.currentTimeMillis();
        final String device = store.deviceName();

        /* THE FOLDER: ONE RECORD PER FILE, read strictly — the transport throws rather than
         * answering empty, and there is no per-device view that can be partial or stale. An era
         * shift (the pair was retired/re-added elsewhere) voids this device's journal INSIDE
         * readState, before it is loaded here — a journal of a dead world read as live is the
         * ghost that minted 373 conflicts on the real phone. */
        StateSet st = readState(net, sec, mk, store, f.key);
        Map<String, Map<String, Object>> state = st.state;
        Map<String, Map<String, Object>> index = store.base(f.key);
        {
            Set<String> devs = new LinkedHashSet<String>();
            for (Map<String, Object> e : state.values()) {
                String by = Json.str(e.get("by"), "");
                if (!by.isEmpty()) devs.add(by);
            }
            devs.add(device);
            rep.devices = devs.size();
        }

        /* AN EMPTY JOURNAL FORCES A HASH, IT DOES NOT STOP THE SWEEP.
         *
         * The first version deferred instead, and the consequence was that the native path could
         * never run ONCE on any device: this journal is written only by this sweep, the page keeps
         * its own in IndexedDB where Java cannot reach it, so it was empty for ever and every alarm
         * answered "first sync — open the app once". Opening the app did nothing, because the page
         * writes only its own copy. Dead on arrival, and reported as success.
         *
         * With no journal both sides look changed for every path at once, so the answer has to come
         * from CONTENT — that is the hash. Otherwise hashing is the charging-time job `shouldSync`
         * already decided about. */
        /* COVERAGE, not emptiness — a first sweep interrupted anywhere leaves a handful of journal
         * entries behind, and "empty" then never fires again: the conflict storm's back door. */
        boolean thin = index.isEmpty()
                || (!state.isEmpty() && index.size() < state.size() / 2);
        if (thin) { hash = true; rep.hashed = true; }
        SafFs.Scan scan = fs.scan(hash, 0, f.excludes);
        Map<String, Map<String, Object>> local = new LinkedHashMap<String, Map<String, Object>>();
        for (Map.Entry<String, Map<String, Object>> e : scan.files.entrySet()) {
            /* The scan reports the FILE's hash in `sha`; an entry's `sha` is the address of its
             * encrypted blob. Renaming it to `csum` here is what stops the engine ever comparing the
             * two — which it used to, and called every identical file divergent. */
            Map<String, Object> src = e.getValue();
            Map<String, Object> out = new LinkedHashMap<String, Object>();
            out.put("size", src.get("size"));
            out.put("mtime", src.get("mtime"));
            Object sha = src.get("sha");
            if (sha instanceof String && !((String) sha).isEmpty()) out.put("csum", sha);
            local.put(normPath(e.getKey()), out);
        }

        SyncReconcile.Plan planned = SyncReconcile.plan(local, state, index, f.excludes, device, now);
        heal(planned, state, local, fs, device, rep);
        List<Map<String, Object>> verdicts = SyncReconcile.check(planned, state);
        /* NOBODY IS WATCHING, SO A REFUSAL IS THE ANSWER — and it suppresses one bucket, never the
         * sweep. The refused paths are deliberately NOT journalled, so the next sweep proposes
         * exactly this again and a foreground one can ask. */
        for (Map<String, Object> vd : verdicts) {
            String kind = Json.str(vd.get("kind"), "");
            if ("massTrash".equals(kind)) rep.refusedTrash = kind;
            else if ("massResurrect".equals(kind)) rep.refusedResurrect = kind;
            else if ("massTombstone".equals(kind)) rep.refusedRemoteDelete = kind;
        }
        SyncReconcile.Plan plan = SyncReconcile.apply(planned, verdicts);
        rep.unchanged = plan.unchanged;
        rep.excluded = plan.excluded;
        rep.deferred += plan.keepBoth.size();
        if (!plan.keepBoth.isEmpty()) {
            rep.deferredWhy = plan.keepBoth.size() + " conflict"
                    + (plan.keepBoth.size() == 1 ? "" : "s") + " need the app open";
        }

        Journal j = new Journal(net, store, sec, mk, f.key, device, st.era, index, rep);

        /* GIVE EVERY ENTRY A CONTENT IDENTITY WHILE WE ARE HERE.
         *
         * An entry written before `csum` existed can never be compared by content, so every device
         * that hashes falls back to size+mtime — which Android can never match, because SAF assigns
         * its own last-modified. Those paths conflict for ever, and a conflict is what this sweep
         * DEFERS, so without this a folder full of them is re-swept every sixteen minutes and settles
         * nothing. It is not a new fact — it is the one this sweep just established, published where
         * the other devices can use it. */
        int repaired = 0;
        for (Map.Entry<String, Map<String, Object>> e : local.entrySet()) {
            Map<String, Object> L = e.getValue(), R = state.get(e.getKey());
            if (L == null || Json.str(L.get("csum"), "").isEmpty()) continue;
            if (R == null || R.get("csum") != null || SyncDiff.gone(R)) continue;
            if (!SyncDiff.same(L, R)) continue;
            Map<String, Object> up = new LinkedHashMap<String, Object>(R);
            up.put("csum", L.get("csum"));
            up.put("v", SyncReconcile.versionOf(R) + 1);
            up.put("by", device);
            j.applied(e.getKey(), up, L, true);
            repaired++;
        }
        rep.repaired = repaired;

        /* DELETIONS FIRST, BEFORE ANY BYTE MOVES. Queued behind hours of transfer they are simply
         * never reached: a sweep is interrupted, restarts its transfer loops from the top, and the
         * deletions sit there across sweep after sweep. A local delete is a rename into .pc-trash —
         * instant, no network — so there is no reason for it to wait behind a 40 GB upload. */
        int ti = 0;
        for (Map<String, Object> t : plan.trash) {
            if (stop != null && stop.stopping()) { j.flush(); return; }
            String path = Json.str(t.get("path"), "");
            progress(f.key, "to trash", path, ++ti, plan.trash.size());
            try {
                fs.trash(path, now);
                Map<String, Object> entry = Json.obj(t.get("entry"));
                /* Applying THEIR tombstone: journal the record as read — nothing to publish. */
                j.applied(path, entry, null, false);
                rep.trashed.add(path);
            } catch (Exception e) { fail(rep, path, "delete", e); }
            j.maybe();
        }

        int di = 0;
        for (Map<String, Object> d : plan.fetch) {
            if (stop != null && stop.stopping()) { j.flush(); return; }
            String path = Json.str(d.get("path"), "");
            Map<String, Object> R = Json.obj(d.get("entry"));
            progress(f.key, "downloading", path, ++di, plan.fetch.size());
            try {
                /* BYTES THIS PHONE ALREADY HOLDS ARE NOT DOWNLOADED AGAIN — the web executor's rule,
                 * mirrored, and it matters more on a radio than anywhere.
                 *
                 * The planner compares content only when BOTH sides carry a checksum, and the scan
                 * does not hash unless it has to. So a record republished with the uploader's own
                 * timestamp looks like a different file to every other device, and each of them
                 * fetches bytes it is already holding. Measured on a desktop: 223 blobs in twelve
                 * minutes, every one a file it already had.
                 *
                 * One local hash instead of a transfer. Equal means we have exactly these bytes:
                 * journal the record against the file that is here and fetch nothing. Anything
                 * else — no checksum, an unreadable hash, a real difference — falls through to the
                 * download unchanged. */
                Map<String, Object> here = local.get(path);
                String want = Json.str(R.get("csum"), "");
                if (!want.isEmpty() && here != null) {
                    String mine = fs.hashFile(path);
                    if (mine != null && mine.equals(want)) {
                        j.applied(path, R, here, false);
                        rep.heldAlready++;
                        j.maybe();
                        continue;
                    }
                }
                long[] got = download(net, fs, mk, path, R, now);
                j.applied(path, R, stat(got, Json.str(R.get("csum"), "")), false);
                rep.downloaded.add(path);
            } catch (Exception e) { fail(rep, path, "download", e); }
            j.maybe();
        }

        int ui = 0;
        for (Map<String, Object> u : plan.send) {
            if (stop != null && stop.stopping()) { j.flush(); return; }
            String path = Json.str(u.get("path"), "");
            Map<String, Object> meta = local.get(path);
            if (meta == null) continue;
            progress(f.key, "uploading", path, ++ui, plan.send.size());
            try {
                /* BYTES THE STORE ALREADY HOLDS ARE NOT UPLOADED AGAIN — the web executor's rule,
                 * mirrored, and it matters more here than anywhere.
                 *
                 * The plan is built from a STAMP (size and mtime), so anything that rewrites a file
                 * without changing a byte — a restore, an rsync, a second sync engine on the same
                 * directory — reads as "changed here". On a phone that means re-sending the file
                 * over a radio, on battery, for no gain; on a multi-gigabyte file it means an upload
                 * that may never finish. One whole-file hash answers it.
                 *
                 * Equal to the live record's checksum means the store holds these exact bytes:
                 * journal the new stamp at the RECORD's own version — not a bump, since this device
                 * learned nothing the folder did not know — and publish nothing. Anything else, or
                 * an unreadable hash, falls through to the upload unchanged: the shortcut can only
                 * remove work, never decide.
                 *
                 * The web side must additionally exempt a named repair, whose whole purpose is to
                 * re-send bytes the STORE lost while the record still certifies them. There is no
                 * such path here — a background sweep does no repairs by name — and if one is ever
                 * added it needs the same exemption. */
                Map<String, Object> R = state.get(path);
                /* A NAMED REPAIR IS EXEMPT FROM THE SHORTCUT. Its whole purpose is to re-send bytes
                 * whose stored copy is unusable while the record still certifies them — "the store
                 * already holds these bytes" is exactly the claim in dispute. */
                if (R != null && R.get("deletedAt") == null && !Json.bool(u.get("resend"), false)) {
                    String was = Json.str(R.get("csum"), "");
                    String is = was.isEmpty() ? null : fs.hashFile(path);
                    if (is != null && is.equals(was)) {
                        j.applied(path, R, meta, false);
                        rep.settledByContent++;
                        j.maybe();
                        continue;
                    }
                }
                Map<String, Object> entry = upload(net, fs, mk, path, meta, device, now, rep);
                entry.put("v", u.get("v"));
                entry.put("by", device);
                j.applied(path, entry, meta, true);
                rep.uploaded.add(path);
            } catch (Exception e) { fail(rep, path, "upload", e); }
            j.maybe();
        }

        for (Map<String, Object> t : plan.tombstone) {
            String path = Json.str(t.get("path"), "");
            /* POSITIVE PROOF, mirroring the JS executor: a deletion is only announced when the
             * exact path is confirmed absent under a healthy parent. Every way a scan fails to
             * SEE used to become a published deletion on every device. */
            boolean[] ev;
            try { ev = fs.confirmGone(path); } catch (Exception e) { ev = new boolean[]{false, false}; }
            if (!ev[0]) {
                rep.unconfirmedAbsent.add(path);
                continue;
            }
            Map<String, Object> tomb = new LinkedHashMap<String, Object>();
            /* The tombstone keeps the file's address — mirrors the JS executor: it is what makes
             * "Restore on every device" possible after the fact. MERGED from the shared record and
             * this device's journal, in that order, never picked: reading the journal alone (which
             * is all this did) published an address-less tombstone for every path whose journal
             * entry had been struck or cleared by an era change — unrestorable account-wide, and
             * unsettleable by any device still holding the file, since delete-loses-to-edit
             * compares csums and an absent csum always reads as an edit. */
            Map<String, Object> shared = state.get(path);
            for (Map<String, Object> src : java.util.Arrays.asList(shared, index.get(path))) {
                if (src == null) continue;
                for (String k : new String[]{"sha", "csum", "size", "mtime", "chunks", "cs", "ps"})
                    if (src.get(k) != null) tomb.put(k, src.get(k));
            }
            tomb.put("v", t.get("v"));
            tomb.put("by", device);
            tomb.put("deletedAt", now);
            j.applied(path, tomb, null, true);
            rep.removedRemote.add(path);
        }

        // Paths the engine settled with no I/O still have to be recorded, or every sweep re-decides
        // them for ever. (`deleted on both`, `same content both sides`, `already gone here`.)
        for (Map<String, Object> n : plan.settle) {
            String path = Json.str(n.get("path"), "");
            j.applied(path, Json.obj(n.get("entry")), local.get(path), false);
        }

        j.flush();
    }

    /** What a file looked like on this disk when we applied something to it — the journal's own
     *  record, which is the only thing a later scan may be compared against. A downloaded file gets
     *  whatever last-modified the platform hands out (SAF assigns its own), so comparing a scan to a
     *  PUBLISHED entry reports every downloaded file as edited, on every sweep, for ever. */
    private static Map<String, Object> stat(long[] st, String csum) {
        Map<String, Object> m = new LinkedHashMap<String, Object>();
        m.put("size", st[0]);
        m.put("mtime", st[1]);
        if (csum != null && !csum.isEmpty()) m.put("csum", csum);
        return m;
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
    private static long[] download(SyncIo.Net net, SyncIo.Files fs, byte[] mk, String path,
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
    /* ONE FILE MUST HAVE ONE SPELLING ON EVERY PLATFORM — the web executor's rule, mirrored,
     * because the record's ADDRESS is sha256(path) and two spellings are two records. Android is not
     * where names decompose (macOS is), but the two halves of this engine must agree about a path or
     * a phone and a laptop publish the same file twice. A no-op for every name either of them has
     * ever produced; it exists so the day a Mac joins is not the day the folder doubles. */
    private static String normPath(String p) {
        String s = String.valueOf(p);
        try { return java.text.Normalizer.normalize(s, java.text.Normalizer.Form.NFC); }
        catch (Exception e) { return s; }
    }

    private static Map<String, Object> upload(SyncIo.Net net, SyncIo.Files fs, byte[] mk, String path,
                                              Map<String, Object> meta, String device, long now,
                                              Report rep) throws Exception {
        long size = Json.num(meta.get("size"), 0);
        long mtime = Json.num(meta.get("mtime"), now);
        Map<String, Object> entry = new LinkedHashMap<String, Object>();
        if (size > CHUNK_ABOVE) {
            List<Object> chunks = new ArrayList<Object>();
            boolean allExisted = true;
            /* THE CHECKSUM MUST CERTIFY THE CHUNKS. The scan's csum (when a rehashing sweep made
             * one) is minutes old by the time the last chunk is read, and a file edited inside that
             * window stores chunks of a TORN file under a clean checksum — every downloader then
             * fails verification for ever while this device's journal says all is well. So the
             * plaintext is digested AS IT IS STORED (free — the bytes are in hand), the scan's csum
             * is checked against that digest, and a mismatch records nothing. */
            java.security.MessageDigest streamDg;
            try { streamDg = java.security.MessageDigest.getInstance("SHA-256"); }
            catch (Exception e) { throw new java.io.IOException("no SHA-256: " + e.getMessage()); }
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
                streamDg.update(plain);
                byte[] blob = SyncCrypto.encrypt(mk, plain);
                String sha = SyncCrypto.sha256hex(blob);
                if (net.blobExists(sha)) { chunks.add(sha); }
                else {
                    /* IT HAS TO LAND WHERE WE COMPUTED IT WOULD. The store is content-addressed, so
                     * the address IS the hash of the bytes — and we hashed them a line ago. A
                     * different answer means the server is holding something other than what was
                     * sent, and recording it writes an entry pointing at bytes that are not this
                     * chunk. Nothing notices until another device downloads it and fails its
                     * checksum, a whole folder of uploads later. (The same check as putParts in
                     * app.js.) */
                    String got = net.putBlob(blob);
                    if (!sha.equals(got)) {
                        throw new java.io.IOException("the server stored a different chunk than the "
                            + "one sent (" + sha.substring(0, 8) + " -> "
                            + (got == null ? "nothing" : got.substring(0, Math.min(8, got.length()))) + ")");
                    }
                    chunks.add(got);
                    allExisted = false;
                }
            }
            if (allExisted) rep.alreadyStored++;
            entry.put("chunks", chunks);
            entry.put("cs", (long) CHUNK_BYTES);
            /* Whatever content identity the scan established travels with it. `chunks` IS an identity
             * on its own, but only at this chunk size — a desktop splitting the same file 16 MB at a
             * time produces a list with nothing in common — so a `csum` is what lets the two agree. It
             * is only here when this was a rehashing sweep; an incremental one leaves it out, exactly
             * as the browser does. */
            String streamed = place.poster.app.signer.Nostr.hex(streamDg.digest());
            String big = Json.str(meta.get("csum"), "");
            if (!big.isEmpty() && !big.equals(streamed)) {
                throw new java.io.IOException("the file changed between the scan and the upload — "
                        + "nothing was recorded; it will be picked up next sweep");
            }
            /* The streamed digest certifies exactly the stored bytes, so it stands in when the scan
             * did not hash — strictly better than publishing no csum at all. */
            entry.put("csum", big.isEmpty() ? streamed : big);
        } else {
            /* A WHOLE-FILE READ IS EXACTLY THE FILE, OR IT IS A FAILURE — the rule the chunked
             * branch above has always had, on the path that did not.
             *
             * This is the worse half, because a truncation here is SELF-CONSISTENT: the short buffer
             * is what gets hashed, so the entry's checksum certifies the truncation, the receiving
             * device verifies it happily, and every check afterwards agrees. A length that disagrees
             * with the scan is also what a file being written while the sweep reads it looks like,
             * and the answer is the same either way: do not store it, report it, take it next
             * sweep. */
            byte[] plain = fs.readAll(path);
            int got = plain == null ? 0 : plain.length;
            if (got != size) {
                throw new java.io.IOException("read " + got + " bytes of " + size
                    + " — the file changed while it was being read, or the read came back short; "
                    + "it will be picked up next sweep");
            }
            byte[] blob = SyncCrypto.encrypt(mk, plain);
            String sha = SyncCrypto.sha256hex(blob);
            if (net.blobExists(sha)) rep.alreadyStored++;
            else {
                String landed = net.putBlob(blob);
                if (!sha.equals(landed)) {
                    throw new java.io.IOException("the server stored a different file than the one "
                        + "sent (" + sha.substring(0, 8) + " -> "
                        + (landed == null ? "nothing" : landed.substring(0, Math.min(8, landed.length()))) + ")");
                }
            }
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

    /**
     * A FLAGGED RECORD, ANSWERED BY WHOEVER STILL HOLDS THE FILE.
     *
     * Another device downloaded these bytes, hashed them, and got something other than the record's
     * checksum. It cannot fix that — it has no good copy — so it writes the flag and the repair
     * belongs here. The web executor has always done this; the background sweep never did, because
     * the envelope's flag was not even carried into the record, so a file uploaded from a phone
     * could only ever be repaired by somebody opening the app.
     *
     * THREE READINGS, and getting the middle one wrong is what strands a file for ever:
     *
     *   - our copy hashes to the record's checksum → the record is right and the STORE's copy is
     *     torn. Re-send: fresh ciphertext, fresh address, and every device's memory of the bad one
     *     lifts by itself.
     *
     *   - our copy hashes to something else, and it is the SAME something the downloader measured
     *     → two independent devices agree about the content and the CHECKSUM is the odd one out.
     *     That is not hypothetical: this app's own digest stopped at a zero-length read and
     *     published the hash of a prefix, so after the fix the holder hashes its own perfectly good
     *     file, disagrees with the number it published, and — on the naive rule — declares its file
     *     bad. The fix would have made the symptom permanent. Re-send under a real checksum.
     *
     *   - our copy hashes to something else again → the two copies really are different and this
     *     one is not evidence about anything. Say so, send nothing.
     *
     * An OLD flag carries no measured hash (just the address), and stays conservative.
     */
    private static void heal(SyncReconcile.Plan plan, Map<String, Map<String, Object>> state,
                             Map<String, Map<String, Object>> local, SyncIo.Files fs, String device,
                             Report rep) {
        Set<String> planned = new LinkedHashSet<String>();
        for (Map<String, Object> u : plan.send) planned.add(Json.str(u.get("path"), ""));
        for (Map.Entry<String, Map<String, Object>> e : state.entrySet()) {
            String path = e.getKey();
            Map<String, Object> R = e.getValue();
            String flag = Json.str(R.get("bad"), "");
            if (flag.isEmpty() || R.get("deletedAt") != null) continue;
            if (planned.contains(path)) continue;          // already going up for its own reasons
            Map<String, Object> L = local.get(path);
            if (L == null) continue;                       // we do not hold it; not ours to repair
            /* The flag names the copy that failed. One naming a DIFFERENT copy from the record's
             * current one has already been answered — somebody re-sent — and repairing again would
             * be the loop the flag exists to end. */
            int bar = flag.indexOf('|');
            String flagged = bar < 0 ? flag : flag.substring(0, bar);
            String saw = bar < 0 ? "" : flag.substring(bar + 1);
            if (!flagged.isEmpty() && !flagged.equals(SyncDiff.addressOf(R))) continue;
            String mine;
            try { mine = fs.hashFile(path); } catch (Exception ex) { mine = null; }
            if (mine == null || mine.isEmpty()) continue;  // could not read it — never a verdict
            String was = Json.str(R.get("csum"), "");
            if (!was.isEmpty() && !mine.equals(was)) {
                if (saw.isEmpty() || !saw.equals(mine)) {
                    rep.badHere.add(path);
                    continue;
                }
                rep.staleChecksum.add(path);
            }
            Map<String, Object> stat = new LinkedHashMap<String, Object>(L);
            stat.put("csum", mine);
            /* `resend` is what makes the upload actually happen: the send loop skips anything whose
             * bytes the store already holds, and for a repair that shortcut is the bug. */
            plan.send.add(SyncReconcile.act("path", path, "v", SyncReconcile.bump(R, null),
                    "stat", stat, "resend", Boolean.TRUE,
                    "why", "another device could not verify this copy — sending it again"));
            rep.reseeding.add(path);
        }
    }

    // ------------------------------------------------------------------------ the record set

    /** The decrypted record set for a pair, plus the era it belongs to. */
    static final class StateSet {
        final Map<String, Map<String, Object>> state = new LinkedHashMap<String, Map<String, Object>>();
        long era = 0;
    }

    /** sha256(path), first 24 hex chars — the record's d-tag suffix, identical to the JS half. */
    static String pathD(String path) {
        String h = SyncCrypto.sha256hex(SyncCrypto.utf8(path));
        return h.substring(0, 24);
    }

    /**
     * The record set: the file-backed cache plus everything written since its last look, decrypted.
     *
     * THROWS when the server cannot be asked — a failed read is never an empty folder. On an ERA
     * SHIFT (the pair was retired or re-added elsewhere) this device's journal answers for records
     * that no longer exist; kept, every path would read "lost record — restoring", which resurrects
     * a folder somebody deliberately retired. So the journal is cleared HERE, before the sweep loads
     * it, and the folder re-settles by content on this very sweep. A record that cannot be decrypted
     * is skipped — the safe direction for one unreadable record is one file the sweep does not move.
     */
    static StateSet readState(SyncIo.Net net, byte[] sec, byte[] mk, SyncStore store, String key)
            throws Exception {
        StateSet out = new StateSet();
        Map<String, Object> cache = store.stateCache(key);
        Long era = null, since = null;
        if (cache != null) {
            era = Json.num(cache.get("era"), 0);
            long cur = Json.num(cache.get("cursor"), 0);
            if (cur > 0) since = Math.max(0, cur - 60);
        }
        Map<String, Object> j = net.state(key, era, since);
        long newEra = Json.num(j.get("era"), 0);
        boolean full = Json.bool(j.get("full"), true);
        Map<String, Object> entries = new LinkedHashMap<String, Object>();
        if (!full && cache != null) entries.putAll(Json.obj(cache.get("entries")));
        if (cache != null && newEra != Json.num(cache.get("era"), 0)) {
            store.saveBase(key, new LinkedHashMap<String, Map<String, Object>>());
        }
        Object recs = j.get("records");
        if (recs instanceof List) {
            for (Object o : (List<?>) recs) {
                Map<String, Object> row = Json.obj(o);
                Map<String, Object> e;
                try {
                    String ct = Json.str(row.get("ct"), "");
                    /* `a1:` = sealed with the DRIVE KEY — hardware AES under a key this device
                     * already holds, never the signer (the pre-a1 seal routed every record through
                     * the signer backend: "about 37 min left" to read a folder). Old records still
                     * open through the fallback. */
                    String json;
                    if (ct.startsWith("a1:")) {
                        byte[] raw = java.util.Base64.getDecoder().decode(ct.substring(3));
                        json = SyncCrypto.fromUtf8(SyncCrypto.decrypt(mk, raw));
                    } else {
                        json = SyncCrypto.openFromSelf(sec, ct);
                    }
                    e = Json.obj(Json.parse(json));
                } catch (Exception ex) { continue; }
                String path = Json.str(e.get("path"), "");
                if (path.isEmpty() || !pathD(path).equals(Json.str(row.get("d"), ""))) continue;
                e.remove("path");
                String ps = Json.str(e.get("ps"), "");
                if (!ps.isEmpty() && !(e.get("chunks") instanceof List)) {
                    /* Resolve the sealed chunk list; one that cannot be fetched makes this record
                     * unreadable (skipped, nothing moved), never address-less. */
                    try {
                        Map<String, Object> raw = Json.obj(Json.parse(SyncCrypto.fromUtf8(
                                SyncCrypto.decrypt(mk, net.getBlob(ps)))));
                        Object cl = raw.get("chunks");
                        if (!(cl instanceof List) || ((List<?>) cl).isEmpty()) continue;
                        e.put("chunks", cl);
                        if (raw.get("cs") != null) e.put("cs", raw.get("cs"));
                    } catch (Exception ex) { continue; }
                }
                // The ENVELOPE is authoritative for version and device — it is what the CAS judged.
                e.put("v", Json.num(row.get("v"), 0));
                /* AND FOR THE FLAG, which this half used to drop on the floor. `bad` is another
                 * device saying "the copy in the store fails its checksum", and it is addressed to
                 * whoever still holds the file — which, for anything uploaded from a phone, is this
                 * device. Never carried across, the background sweep could not repair anything: the
                 * flag sat on the record, the file stayed unfetchable everywhere, and only opening
                 * the app could fix it. */
                String flag = Json.str(row.get("bad"), "");
                if (!flag.isEmpty()) e.put("bad", flag);
                String by = Json.str(row.get("by"), "");
                if (!by.isEmpty()) e.put("by", by);
                if (Json.num(row.get("t"), 0) != 0 && Json.num(e.get("deletedAt"), 0) == 0) {
                    e.put("deletedAt", Json.num(row.get("at"), 1));
                }
                entries.put(path, e);
            }
        }
        Map<String, Object> save = new LinkedHashMap<String, Object>();
        save.put("era", newEra);
        save.put("cursor", Json.num(j.get("now"), 0));
        save.put("entries", entries);
        store.saveStateCache(key, save);
        out.era = newEra;
        for (Map.Entry<String, Object> e : entries.entrySet()) {
            out.state.put(e.getKey(), Json.obj(e.getValue()));
        }
        return out;
    }

    /**
     * What this device has applied, and the records this sweep must publish.
     *
     * THE RECORDS GO FIRST, ALWAYS: a journal persisted ahead of the published records is a device
     * that believes in an agreement no other device has seen; records ahead of the journal are safe
     * — the next sweep adopts its own publish by content. A record the server REFUSES (another
     * device won that file's version) is struck from the journal on the spot: this device then
     * honestly knows nothing about the path, and the next sweep resolves the divergence as a
     * conflict, with both copies surviving.
     */
    private static final class Journal {
        private final SyncIo.Net net;
        private final SyncStore store;
        private final byte[] sec, mk;
        private final String key, me;
        private final long era;
        private final Map<String, Map<String, Object>> index;
        private final List<Map<String, Object>> pending = new ArrayList<Map<String, Object>>();
        private final Report rep;
        private boolean dirty = false;
        private int since = 0;

        Journal(SyncIo.Net net, SyncStore store, byte[] sec, byte[] mk, String key, String me,
                long era, Map<String, Map<String, Object>> index, Report rep) {
            this.net = net; this.store = store; this.sec = sec; this.mk = mk;
            this.key = key; this.me = me; this.era = era; this.index = index; this.rep = rep;
        }

        private static Map<String, Object> strip(Map<String, Object> e) {
            Map<String, Object> c = new LinkedHashMap<String, Object>(e);
            c.remove("local");
            return c;
        }

        /** This path is settled at `entry`; `publish` when this sweep MADE the change. */
        void applied(String path, Map<String, Object> entry, Map<String, Object> local,
                     boolean publish) {
            Map<String, Object> keep = new LinkedHashMap<String, Object>(entry);
            if (local != null) keep.put("local", local); else keep.remove("local");
            index.put(path, keep);
            if (publish) {
                Map<String, Object> row = new LinkedHashMap<String, Object>();
                row.put("path", path);
                row.put("entry", strip(entry));
                pending.add(row);
            }
            dirty = true;
        }

        /** A FAILED CHECKPOINT IS NOT A FAILED SWEEP — the work is real either way. */
        void maybe() {
            if (!dirty || ++since < CHECKPOINT) return;
            since = 0;
            try { write(); rep.checkpoints++; }
            catch (Exception e) { rep.error = "checkpoint: " + e.getMessage(); }
        }

        /** The final write is deliberately NOT a checkpoint: this one failing means the sweep's whole
         *  result was never recorded, so it throws. */
        void flush() throws Exception {
            if (!dirty) return;
            write();
            dirty = false;
        }

        private void write() throws Exception {
            if (!pending.isEmpty()) {
                List<Map<String, Object>> batch = new ArrayList<Map<String, Object>>(pending);
                pending.clear();
                for (int at = 0; at < batch.size(); at += 400) {
                    List<Object> put = new ArrayList<Object>();
                    Map<String, String> byD = new LinkedHashMap<String, String>();
                    int end = Math.min(batch.size(), at + 400);
                    for (int i = at; i < end; i++) {
                        Map<String, Object> r = batch.get(i);
                        String path = Json.str(r.get("path"), "");
                        Map<String, Object> entry = new LinkedHashMap<String, Object>(Json.obj(r.get("entry")));
                        /* A chunk list too big for the seal moves into its own encrypted blob —
                         * mirrors sync.js: an Android-chunked file past ~4 GB lists more chunks
                         * than NIP-44 will seal. */
                        Object chunks = entry.get("chunks");
                        if (chunks instanceof List && Json.write(chunks).length() > 58000) {
                            Map<String, Object> sealed = new LinkedHashMap<String, Object>();
                            sealed.put("chunks", chunks);
                            sealed.put("cs", entry.get("cs"));
                            String ps = net.putBlob(SyncCrypto.encrypt(mk,
                                    SyncCrypto.utf8(Json.write(sealed))));
                            entry.put("ps", ps);
                            entry.put("pn", (long) ((List<?>) chunks).size());
                            entry.remove("chunks");
                        }
                        Map<String, Object> withPath = new LinkedHashMap<String, Object>();
                        withPath.put("path", path);
                        withPath.putAll(entry);
                        String d = pathD(path);
                        byD.put(d, path);
                        Map<String, Object> row = new LinkedHashMap<String, Object>();
                        row.put("d", d);
                        row.put("v", SyncReconcile.versionOf(entry));
                        row.put("by", me);
                        row.put("ct", "a1:" + java.util.Base64.getEncoder().encodeToString(
                                SyncCrypto.encrypt(mk, SyncCrypto.utf8(Json.write(withPath)))));
                        if (Json.num(entry.get("deletedAt"), 0) != 0) row.put("t", 1L);
                        put.add(row);
                    }
                    /* The native sweep never carries a person, so it never passes `confirmed` —
                     * the server's tombstone backstop stands at full strength behind it. */
                    Map<String, Object> ans = net.putState(key, era, put, false);
                    Object results = ans.get("results");
                    if (results instanceof List) {
                        for (Object o : (List<?>) results) {
                            Map<String, Object> res = Json.obj(o);
                            if (Json.bool(res.get("stale"), false)) {
                                String path = byD.get(Json.str(res.get("d"), ""));
                                if (path != null) index.remove(path);
                            }
                        }
                    }
                }
            }
            store.saveBase(key, index);
        }
    }
}
