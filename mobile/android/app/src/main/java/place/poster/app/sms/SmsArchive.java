package place.poster.app.sms;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;

import org.json.JSONObject;
import org.json.JSONArray;

import java.util.ArrayList;
import java.util.List;

import place.poster.app.signer.Crypt;
import place.poster.app.signer.Nostr;
import place.poster.app.signer.SignerKey;
import place.poster.app.sync.SyncCrypto;
import place.poster.app.sync.SyncNet;
import place.poster.app.sync.SyncStore;

/**
 * SmsSweep, bound to an actual phone.
 *
 * The sweep itself takes every side effect as an interface so it can be RUN off a handset
 * (tests/test_android_sms_sweep.py drives a whole pass against a HashMap provider). This is the
 * other half: the provider, the encrypted drive and the Keystore-sealed key, in one place, so the
 * three things only a phone can answer are the three things not under test.
 *
 * NOTHING HERE PUBLISHES. `sweep()` hands back signed events and SignerRelayService — the only
 * thing that knows whether a relay is actually connected — waits for every relay receipt before committing the mark.
 */
public final class SmsArchive {
    private static final String TAG = "PCSmsArchive";
    private static final String PREFS = "pcsms_archive";
    private static final String K_LAST = "last";
    private static final Object CHECKPOINT_LOCK = new Object();

    /** One pass, bounded: this runs while somebody is holding the phone. */
    public static final int ROWS_PER_PASS = 25;

    private static final int KIND = 30078;
    private static final String L_TAG = "pcai-sms";

    private SmsArchive() { }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** How far the archive has got, in provider milliseconds. */
    private static String owner(Context ctx) {
        try { byte[] key = SignerKey.load(ctx); return key == null ? "" : Nostr.hex(Nostr.pubkey(key)); }
        catch (Throwable ignored) { return ""; }
    }
    // v3 starts a repair pass: the old global cursor could advance past rejected socket writes.
    // Account-specific cursors never hide one account's history after switching signing keys.
    private static String markKey(String owner) { return "mark.v3:" + owner; }
    private static String pendingKey(String owner) { return "pending.v3:" + owner; }
    public static long mark(Context ctx) { return prefs(ctx).getLong(markKey(owner(ctx)), 0L); }

    /** What the last pass did, for the panel that has to explain a phone nobody can query. */
    public static String last(Context ctx) { return prefs(ctx).getString(K_LAST, ""); }

    /**
     * Start again from the beginning.
     *
     * The mark is the only thing that makes an attachment the provider refused permanent — the row
     * is archived naming the reason and the mark moves past it, which is what stops ten refusals at
     * the old end of the store standing in front of every newer message. Re-reading them is
     * therefore a deliberate act, never something a sweep decides for itself.
     */
    public static void rescan(Context ctx) {
        synchronized (CHECKPOINT_LOCK) {
            String owner = owner(ctx);
            prefs(ctx).edit().putLong("revision.v3:" + owner, prefs(ctx).getLong("revision.v3:" + owner, 0L) + 1L)
                    .putLong(markKey(owner), 0L).remove("sms.v3:" + owner).remove("mms.v3:" + owner)
                    .remove(pendingKey(owner)).commit();
        }
    }

    /**
     * Record why a pass did not happen.
     *
     * A sweep that is never asked for and a sweep that found nothing look identical from every
     * screen — and the one thing this feature has been reported as, over and over, is "it says it
     * synced and the messages are not there". Whatever the answer is, it is written down.
     */
    public static void note(Context ctx, String line) {
        prefs(ctx).edit().putString(K_LAST, line).apply();
    }

    /** Build one window of archive events, or null when this phone cannot archive at all. */
    public static SmsSweep.Report sweep(Context ctx, int maxRows) {
        final byte[] sec = SignerKey.load(ctx);
        if (sec == null) {
            note(ctx, "not archiving: this phone holds no signing key");
            return null;
        }
        final SyncStore store = new SyncStore(ctx);
        if (store.apiBase().isEmpty() || store.mediaBase().isEmpty()) {
            note(ctx, "not archiving: sign in to an instance once, so this phone knows where the "
                    + "encrypted drive is");
            return null;
        }
        final byte[] me;
        final String pubHex;
        try {
            me = Nostr.pubkey(sec);
            pubHex = SmsOutbox.hex(me);
        } catch (Throwable t) {
            return null;
        }

        final long revision;
        synchronized (CHECKPOINT_LOCK) {
            revision = prefs(ctx).getLong("revision.v3:" + pubHex, 0L);
            String pending = prefs(ctx).getString(pendingKey(pubHex), "");
            if (!pending.isEmpty()) {
                try {
                    SmsSweep.Report restored = restore(pending, pubHex);
                    if (restored.revision == revision) {
                        // SharedPreferences updates memory even when commit() fails. Reaffirm disk
                        // durability before treating a memory-visible retry as publishable.
                        SmsSweep.Report durable = durableRestore(ctx, pubHex, pending);
                        note(ctx, "retrying " + durable.events.size() + " history records; waiting for relay acceptance");
                        return durable;
                    }
                    if (!prefs(ctx).edit().remove(pendingKey(pubHex)).commit()) return null;
                } catch (Throwable invalid) { note(ctx, "archive recovery record is unreadable; rescan required"); return null; }
            }
        }

        final SyncNet net = new SyncNet(store.apiBase(), store.mediaBase(), sec);
        /* THE DRIVE KEY IS RESOLVED ONCE, BEFORE ANY ATTACHMENT IS TOUCHED. Doing it per photo is
         * the same shape as the browser bug this feature shipped alongside: sixteen concurrent
         * reads each raced the key fetch and every one of them failed with a message about the key
         * rather than about the picture. */
        final byte[][] mk = new byte[1][];

        SmsSweep.Io io = new SmsSweep.Io() {
            public List<SmsMsg> since(long dateMs, int limit) {
                List<SmsMsg> rows = Messages.archiveSince(ctx, dateMs, smsAtMark(), mmsAtMark(), limit);
                return rows == null ? new ArrayList<SmsMsg>() : rows;
            }

            public byte[] partBytes(SmsPart part) throws Exception {
                return SmsSweep.readWhole(provider(ctx), part.id);
            }

            public String putBlob(byte[] plain, String mime, String name) throws Exception {
                if (mk[0] == null) {
                    String wrapped = net.driveKey();
                    if (!wrapped.equals(store.wrappedDriveKey())) store.setWrappedDriveKey(wrapped);
                    mk[0] = SyncCrypto.unwrapMasterKey(sec, wrapped);
                }
                /* 12-byte IV then AES-GCM under the master key — byte-identical to app.js's
                 * _masterEncrypt, which is what lets the web client read this back through
                 * encFileUrl(sha, mime) with no files-index entry of its own. */
                return net.putBlob(SyncCrypto.encrypt(mk[0], plain));
            }

            public JSONObject seal(String doc, String bodyJson) throws Exception {
                String ct = Crypt.nip44Encrypt(Crypt.conversationKey(sec, me), bodyJson, null);
                List<List<String>> tags = new ArrayList<List<String>>();
                List<String> d = new ArrayList<String>(); d.add("d"); d.add(doc); tags.add(d);
                List<String> l = new ArrayList<String>(); l.add("l"); l.add(L_TAG); tags.add(l);
                return SmsOutbox.signed(sec, pubHex, System.currentTimeMillis() / 1000L,
                        KIND, tags, ct);
            }

            public String contactName(String address) { return PhoneBook.nameOf(ctx, address); }
            public long smsAtMark() { return prefs(ctx).getLong("sms.v3:" + pubHex, -1L); }
            public long mmsAtMark() { return prefs(ctx).getLong("mms.v3:" + pubHex, -1L); }
            public long mark() { return prefs(ctx).getLong(markKey(pubHex), 0L); }

            public void mark(long dateMs) {
                prefs(ctx).edit().putLong(markKey(pubHex), dateMs).apply();
            }
        };

        SmsSweep.Report rep = SmsSweep.run(io, maxRows <= 0 ? ROWS_PER_PASS : maxRows);
        rep.owner = pubHex; rep.revision = revision;
        synchronized (CHECKPOINT_LOCK) {
            if (!pubHex.equals(owner(ctx)) || revision != prefs(ctx).getLong("revision.v3:" + pubHex, 0L)) return null;
            if (!rep.events.isEmpty()) {
                try {
                    // Persist exact signed events BEFORE touching any socket. Retry after process death
                    // uses the same ids and never repeats a radio send (this is archive-only).
                    if (!prefs(ctx).edit().putString(pendingKey(pubHex), encode(rep)).commit()) {
                        note(ctx, "archive pending batch could not be saved"); return null;
                    }
                } catch (Throwable failed) { note(ctx, "archive pending batch could not be saved"); return null; }
            } else if (rep.skipped > 0 && rep.error.isEmpty()) {
                // No-address provider rows have no document to ACK. Record only scan progress,
                // without calling them published, so a full page cannot hide later valid messages.
                if (!prefs(ctx).edit().putLong(markKey(pubHex), rep.mark)
                        .putLong("sms.v3:" + pubHex, rep.smsAtMark).putLong("mms.v3:" + pubHex, rep.mmsAtMark).commit()) {
                    note(ctx, "archive scan position could not be saved"); return null;
                }
            }
        }
        record(ctx, rep);
        return rep;
    }

    private static SmsSweep.Report durableRestore(Context ctx, String owner, String raw) throws Exception {
        SmsSweep.Report rep = restore(raw, owner);
        if (!prefs(ctx).edit().putString(pendingKey(owner), raw).commit())
            throw new IllegalStateException("archive pending batch is not durable");
        return rep;
    }

    private static String encode(SmsSweep.Report rep) throws Exception {
        JSONArray events = new JSONArray();
        for (JSONObject event : rep.events) events.put(event);
        return new JSONObject().put("owner", rep.owner).put("revision", rep.revision).put("mark", rep.mark)
                .put("sms", rep.smsAtMark).put("mms", rep.mmsAtMark)
                .put("more", rep.more).put("events", events).toString();
    }

    private static SmsSweep.Report restore(String raw, String owner) throws Exception {
        JSONObject saved = new JSONObject(raw);
        if (!owner.equals(saved.optString("owner", ""))) throw new Exception("wrong owner");
        SmsSweep.Report rep = new SmsSweep.Report();
        rep.owner = owner; rep.revision = saved.optLong("revision", 0L); rep.mark = saved.optLong("mark", 0L);
        rep.smsAtMark = saved.optLong("sms", -1L); rep.mmsAtMark = saved.optLong("mms", -1L);
        rep.more = saved.optBoolean("more", false);
        JSONArray events = saved.getJSONArray("events");
        if (events.length() == 0 || events.length() > ROWS_PER_PASS) throw new Exception("invalid batch");
        for (int i = 0; i < events.length(); i++) {
            JSONObject event = events.getJSONObject(i);
            if (!owner.equals(event.optString("pubkey", "")) || !event.optString("id", "").matches("[0-9a-f]{64}"))
                throw new Exception("invalid event");
            rep.events.add(event);
        }
        return rep;
    }

    /** A rescan invalidates in-flight delivery as well as the persistent checkpoint. */
    public static boolean current(Context ctx, SmsSweep.Report rep) {
        synchronized (CHECKPOINT_LOCK) {
            if (rep == null || !rep.owner.equals(owner(ctx)) || rep.events.isEmpty()
                    || rep.revision != prefs(ctx).getLong("revision.v3:" + rep.owner, 0L)) return false;
            try {
                SmsSweep.Report saved = restore(prefs(ctx).getString(pendingKey(rep.owner), ""), rep.owner);
                return saved.revision == rep.revision && saved.events.get(0).optString("id")
                        .equals(rep.events.get(0).optString("id"));
            } catch (Throwable ignored) { return false; }
        }
    }

    /** Called only after every event received a positive relay OK, never after WebSocket.send. */
    public static void commit(Context ctx, SmsSweep.Report rep) {
        synchronized (CHECKPOINT_LOCK) {
            if (rep == null || rep.events.isEmpty() || !rep.owner.equals(owner(ctx))
                    || rep.revision != prefs(ctx).getLong("revision.v3:" + rep.owner, 0L)) return;
            try {
                SmsSweep.Report pending = restore(prefs(ctx).getString(pendingKey(rep.owner), ""), rep.owner);
                // A user rescan cancels the previous batch; a delayed ACK cannot advance its new cursor.
                if (!pending.events.get(0).optString("id").equals(rep.events.get(0).optString("id"))) return;
                if (!prefs(ctx).edit().putLong(markKey(rep.owner), Math.max(rep.mark, mark(ctx)))
                        .putLong("sms.v3:" + rep.owner, rep.smsAtMark).putLong("mms.v3:" + rep.owner, rep.mmsAtMark)
                        .remove(pendingKey(rep.owner)).commit()) note(ctx, "relay accepted history; checkpoint save failed, retrying");
                else note(ctx, "relay accepted " + rep.events.size() + " history records");
            } catch (Throwable ignored) { }
        }
    }

    /**
     * WHAT THE PHONE MEASURED, kept where a person can read it.
     *
     * The handset is the only device that knows why a picture is not in the archive, and it had no
     * way to say so: a document flagged `mms:true` carrying no attachment looks the same from every
     * other screen whether the provider refused it, the file was too large, or it was never tried.
     */
    private static void record(Context ctx, SmsSweep.Report rep) {
        if (rep == null) return;
        String line = "rows=" + rep.rows + " prepared=" + rep.published
                + " skipped-no-address=" + rep.skipped
                + " attachments=" + rep.attachments + " refused=" + rep.refused
                + (!rep.events.isEmpty() ? " waiting for relay acceptance; retrying if needed" : "")
                + (rep.more ? " more" : "")
                + (rep.error.isEmpty() ? "" : " error=" + rep.error);
        prefs(ctx).edit().putString(K_LAST, line).apply();
        Log.i(TAG, "sms archive: " + line);
    }

    /** The provider reads, behind the interface the paging logic is tested against. */
    private static SmsSweep.Parts provider(final Context ctx) {
        return new SmsSweep.Parts() {
            public byte[] bytes(long partId, int maxBytes) {
                return MmsStore.partBytes(ctx, partId, maxBytes);
            }
            public byte[] chunk(long partId, long offset, int maxBytes) {
                return MmsStore.partChunk(ctx, partId, offset, maxBytes);
            }
            public long size(long partId) {
                return MmsStore.sizeOf(ctx, partId);
            }
        };
    }
}
