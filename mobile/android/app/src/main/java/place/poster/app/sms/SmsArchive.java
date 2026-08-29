package place.poster.app.sms;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;

import org.json.JSONObject;

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
 * thing that knows whether a relay is actually connected — sends them and then commits the mark.
 */
public final class SmsArchive {
    private static final String TAG = "PCSmsArchive";
    private static final String PREFS = "pcsms_archive";
    private static final String K_MARK = "mark";
    private static final String K_LAST = "last";

    /** One pass, bounded: this runs while somebody is holding the phone. */
    public static final int ROWS_PER_PASS = 25;

    private static final int KIND = 30078;
    private static final String L_TAG = "pcai-sms";

    private SmsArchive() { }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** How far the archive has got, in provider milliseconds. */
    public static long mark(Context ctx) { return prefs(ctx).getLong(K_MARK, 0L); }

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
        prefs(ctx).edit().putLong(K_MARK, 0L).apply();
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

        final SyncNet net = new SyncNet(store.apiBase(), store.mediaBase(), sec);
        /* THE DRIVE KEY IS RESOLVED ONCE, BEFORE ANY ATTACHMENT IS TOUCHED. Doing it per photo is
         * the same shape as the browser bug this feature shipped alongside: sixteen concurrent
         * reads each raced the key fetch and every one of them failed with a message about the key
         * rather than about the picture. */
        final byte[][] mk = new byte[1][];

        SmsSweep.Io io = new SmsSweep.Io() {
            public List<SmsMsg> since(long dateMs, int limit) {
                List<SmsMsg> rows = Messages.since(ctx, dateMs, limit);
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

            public long mark() { return SmsArchive.mark(ctx); }

            public void mark(long dateMs) {
                prefs(ctx).edit().putLong(K_MARK, dateMs).apply();
            }
        };

        SmsSweep.Report rep = SmsSweep.run(io, maxRows <= 0 ? ROWS_PER_PASS : maxRows);
        record(ctx, rep);
        return rep;
    }

    /** Move the mark, once the caller has actually put these events on a socket. */
    public static void commit(Context ctx, SmsSweep.Report rep) {
        if (rep != null && rep.mark > mark(ctx)) {
            prefs(ctx).edit().putLong(K_MARK, rep.mark).apply();
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
        String line = "rows=" + rep.rows + " published=" + rep.published
                + " attachments=" + rep.attachments + " refused=" + rep.refused
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
