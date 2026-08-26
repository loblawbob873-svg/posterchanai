package place.poster.app.sms;

import android.content.Context;
import android.util.Log;

import org.json.JSONArray;
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
 * SENDING A TEXT THIS PHONE WAS ASKED FOR, WITHOUT THE APP BEING OPEN.
 *
 * Another device -- a laptop, the desktop -- cannot reach a radio, so it writes an encrypted request
 * at `pcai:smsout:<id>` and the handset performs it. That half already existed and worked, with one
 * limit that made it close to useless: the drain lives in the client's JavaScript and runs on `load`
 * and on `visibilitychange`, so the phone only performed a request when somebody OPENED PosterChan
 * on it. Reported as "it should not have to be visible" -- and it should not.
 *
 * This is the same drain in Java, driven by the signer service's relay socket, which is already up
 * whenever the phone is acting as a remote signer.
 *
 * <h3>The double-send, and why this is not simply a second reader</h3>
 *
 * A sent text cannot be unsent. The JS drain SENDS and then marks the request done, so a native
 * drain reading the same document a moment earlier would send it too, and somebody's message goes
 * out twice with no way to take it back.
 *
 * They are not coordinated by a claim on the relay, because that is a distributed agreement between
 * two halves of ONE device and there is a local answer: they never run at the same time. The JS
 * drain runs when the app is visible; this one refuses when the app is visible. `AppVisible` is
 * written by the Activity's own lifecycle, so the question is answered by Android rather than
 * inferred.
 *
 * <h3>What it will not do</h3>
 *
 * A request older than a day is dropped rather than performed -- a text that arrives a day late is
 * worse than one that never went -- and the marker is written whether the send succeeded or failed,
 * because a retry of a send that already reached the network is the one mistake with no undo.
 */
public final class SmsOutbox {
    private static final String CLAIMS = "poster_sms_outbox_claims";
    private static final String CANCELLED = "poster_sms_outbox_cancelled";

    private static final String TAG = "PosterChan";
    private static final int KIND = 30078;
    private static final String L_TAG = "pcai-sms";
    private static final String D_OUT = "pcai:smsout:";
    /** A day. The client uses the same bound; see its MAX_AGE_MS. */
    private static final long MAX_AGE_MS = 86400000L;
    /** One pass is a handful of requests. A phone that has been off for a week is not a backlog. */
    private static final int MAX_PER_PASS = 10;

    private SmsOutbox() { }

    /** Build the encrypted archive event for a text that arrived while the WebView was asleep. */
    public static JSONObject archiveIncoming(Context ctx, String from, String body, long when) {
        try {
            byte[] sec = SignerKey.load(ctx);
            if (sec == null || from == null || from.isEmpty()) return null;
            byte[] me = Nostr.pubkey(sec);
            JSONObject o = new JSONObject();
            o.put("address", from); o.put("body", body == null ? "" : body);
            o.put("date", when); o.put("incoming", true); o.put("name", "");
            byte[] ck = Crypt.conversationKey(sec, me);
            String ct = Crypt.nip44Encrypt(ck, o.toString(), null);
            String doc = SmsKeys.docId(from, when, body, true);
            List<List<String>> tags = new ArrayList<List<String>>();
            List<String> d = new ArrayList<String>(); d.add("d"); d.add(doc); tags.add(d);
            List<String> l = new ArrayList<String>(); l.add("l"); l.add(L_TAG); tags.add(l);
            long now = System.currentTimeMillis() / 1000L;
            String pubHex = hex(me), tagsJson = Nostr.tagsJson(tags);
            String ser = Nostr.serialize(pubHex, now, KIND, tagsJson, ct);
            byte[] id = Nostr.sha256(ser.getBytes("UTF-8"));
            JSONObject out = new JSONObject();
            out.put("id", hex(id)); out.put("pubkey", pubHex); out.put("created_at", now);
            out.put("kind", KIND); out.put("tags", new JSONArray(tagsJson)); out.put("content", ct);
            out.put("sig", hex(Nostr.sign(id, sec, null)));
            return out;
        } catch (Throwable t) {
            Log.w(TAG, "sms archive: could not seal incoming message", t);
            return null;
        }
    }

    /** Replace an archived message with a durable tombstone after Android deleted its provider row. */
    public static List<JSONObject> archiveDelete(Context ctx, String doc) {
        List<JSONObject> out = new ArrayList<JSONObject>();
        try {
            byte[] sec = SignerKey.load(ctx);
            if (sec == null || doc == null || doc.isEmpty()) return out;
            byte[] me = Nostr.pubkey(sec);
            String pubHex = hex(me);
            long now = System.currentTimeMillis() / 1000L;

            List<List<String>> tags = new ArrayList<List<String>>();
            List<String> d = new ArrayList<String>(); d.add("d"); d.add(doc); tags.add(d);
            List<String> l = new ArrayList<String>(); l.add("l"); l.add(L_TAG); tags.add(l);
            out.add(signed(sec, pubHex, now, KIND, tags, ""));

            List<List<String>> delTags = new ArrayList<List<String>>();
            List<String> a = new ArrayList<String>();
            a.add("a"); a.add(KIND + ":" + pubHex + ":" + doc); delTags.add(a);
            out.add(signed(sec, pubHex, now, 5, delTags, ""));
        } catch (Throwable t) {
            Log.w(TAG, "sms archive: could not seal deletion", t);
            out.clear();
        }
        return out;
    }

    private static JSONObject signed(byte[] sec, String pubHex, long at, int kind,
                                     List<List<String>> tags, String content) throws Exception {
        String tagsJson = Nostr.tagsJson(tags);
        String ser = Nostr.serialize(pubHex, at, kind, tagsJson, content);
        byte[] id = Nostr.sha256(ser.getBytes("UTF-8"));
        JSONObject ev = new JSONObject();
        ev.put("id", hex(id)); ev.put("pubkey", pubHex); ev.put("created_at", at);
        ev.put("kind", kind); ev.put("tags", new JSONArray(tagsJson)); ev.put("content", content);
        ev.put("sig", hex(Nostr.sign(id, sec, null)));
        return ev;
    }

    /** The REQ this drain needs on a relay socket the caller already owns. */
    public static JSONObject filter(String mePubHex) throws Exception {
        JSONObject f = new JSONObject();
        f.put("kinds", new JSONArray().put(KIND));
        f.put("authors", new JSONArray().put(mePubHex));
        f.put("#l", new JSONArray().put(L_TAG));
        f.put("limit", 200);
        return f;
    }

    /** Is this event one of ours to act on? Cheap, and called for every event on the socket. */
    public static boolean isRequest(JSONObject ev) {
        try {
            JSONArray tags = ev.optJSONArray("tags");
            if (tags == null) return false;
            for (int i = 0; i < tags.length(); i++) {
                JSONArray t = tags.optJSONArray(i);
                if (t != null && t.length() >= 2 && "d".equals(t.optString(0))
                        && t.optString(1).startsWith(D_OUT)) return true;
            }
        } catch (Throwable ignored) { }
        return false;
    }

    private static String docOf(JSONObject ev) {
        JSONArray tags = ev.optJSONArray("tags");
        if (tags == null) return "";
        for (int i = 0; i < tags.length(); i++) {
            JSONArray t = tags.optJSONArray(i);
            if (t != null && t.length() >= 2 && "d".equals(t.optString(0))) return t.optString(1);
        }
        return "";
    }

    /**
     * Atomically reserve an outbox document before touching the radio.  Both the foreground
     * Capacitor plugin and the background relay service run in this process, so synchronizing this
     * persisted ledger closes the onResume/onPause race without making delivery depend on timing.
     * Claims deliberately survive process death: after SmsManager has accepted a message it is
     * safer to report an interrupted receipt than to transmit that message a second time.
     */
    public static synchronized boolean claim(Context ctx, String doc) {
        if (doc == null || !doc.startsWith("pcai:smsout:")) return false;
        android.content.SharedPreferences p = ctx.getSharedPreferences(CLAIMS, Context.MODE_PRIVATE);
        if (p.contains(doc)) return false;
        return p.edit().putLong(doc, System.currentTimeMillis()).commit();
    }

    /** Remember a sender cancellation even when a worker has already claimed the request. */
    public static synchronized void cancel(Context ctx, String doc) {
        if (doc == null || !doc.startsWith(D_OUT)) return;
        ctx.getSharedPreferences(CANCELLED, Context.MODE_PRIVATE).edit()
                .putLong(doc, System.currentTimeMillis()).commit();
    }

    public static synchronized boolean isCancelled(Context ctx, String doc) {
        return doc != null && ctx.getSharedPreferences(CANCELLED, Context.MODE_PRIVATE)
                .contains(doc);
    }

    /**
     * Perform one request. Returns the EVENT to publish (the done marker), or null when there is
     * nothing to do -- already done, too old, not for us, or the app is on screen and its own drain
     * owns this.
     *
     * The caller publishes: this class does not own a socket, which keeps it testable without one.
     */
    public static JSONObject perform(Context ctx, JSONObject ev) {
        try {
            if (!isRequest(ev)) return null;
            if (AppVisible.is()) return null;          // the client's own drain has it
            byte[] sec = SignerKey.load(ctx);
            if (sec == null) return null;              // no key here: nothing to decrypt with
            byte[] me = Nostr.pubkey(sec);
            byte[] ck = Crypt.conversationKey(sec, me);   // sealed to the user's OWN key
            String plain = Crypt.nip44Decrypt(ck, ev.optString("content", ""));
            JSONObject req = new JSONObject(plain);
            String doc = docOf(ev);
            if (doc.isEmpty()) return null;
            if (req.optBoolean("done", false)) {
                if (req.optBoolean("cancelled", false)) cancel(ctx, doc);
                return null;
            }
            String to = req.optString("to", "");
            String body = req.optString("body", "");
            JSONObject attachment = req.optJSONObject("attachment");
            if (to.isEmpty() || (body.isEmpty() && attachment == null)) return null;

            long asked = req.optLong("at", 0L);
            if (System.currentTimeMillis() - asked > MAX_AGE_MS) {
                return marker(ctx, sec, me, doc, false, "too old", to, body, asked, attachment);
            }

            // A background MMS needs the account's encrypted-drive endpoints/key. If this phone has
            // never received that config, leave the request unclaimed for the visible WebView.
            SyncStore store = null;
            if (attachment != null) {
                store = new SyncStore(ctx);
                if (store.apiBase().isEmpty() || store.mediaBase().isEmpty()
                        || store.wrappedDriveKey().isEmpty()) return null;
            }
            if (!claim(ctx, doc)) return null;

            SmsSender.Result r;
            if (attachment == null) {
                r = SmsSender.send(ctx, to, body);
            } else {
                try {
                    String sha = attachment.optString("sha", "");
                    if (!sha.matches("[0-9a-fA-F]{64}")) throw new Exception("invalid MMS attachment");
                    long bytes = attachment.optLong("bytes", -1L);
                    if (bytes < 0L || bytes > 8L * 1024L * 1024L)
                        throw new Exception("picture message is too large");
                    SyncNet net = new SyncNet(store.apiBase(), store.mediaBase(), sec);
                    String canonical = net.driveKey();
                    if (!canonical.equals(store.wrappedDriveKey())) store.setWrappedDriveKey(canonical);
                    byte[] mk = SyncCrypto.unwrapMasterKey(sec, canonical);
                    byte[] imageBytes = SyncCrypto.decrypt(mk, net.getBlob(sha));
                    /* Download/decrypt can take seconds. A cancellation event delivered while that
                     * work was running must win before bytes cross the irreversible radio boundary. */
                    if (isCancelled(ctx, doc)) {
                        r = new SmsSender.Result();
                        r.error = "cancelled by sender";
                    } else {
                        r = MmsSender.send(ctx, to, body, imageBytes);
                    }
                } catch (Throwable mmsError) {
                    r = new SmsSender.Result();
                    r.error = mmsError.getMessage() == null ? "could not send picture message"
                                                            : mmsError.getMessage();
                }
            }
            /* MARKED WHETHER IT WENT OR NOT. A text that went out and whose marker did not is a text
             * that goes out AGAIN on the next pass, and there is no undo for that. A failure is
             * recorded in the marker so the other device can say so, rather than retried blindly. */
            long sentAt = r != null && r.sentAt > 0L ? r.sentAt : asked;
            return marker(ctx, sec, me, doc, r != null && r.ok, r == null ? "no result" : r.error,
                    to, body, sentAt, attachment);
        } catch (Throwable t) {
            Log.w(TAG, "sms outbox: could not perform a request", t);
            return null;
        }
    }

    /** The `{done:true}` replacement for the request, signed and ready to publish. */
    private static JSONObject marker(Context ctx, byte[] sec, byte[] me, String doc,
                                     boolean ok, String error, String to, String body,
                                     long asked, JSONObject attachment) throws Exception {
        JSONObject o = new JSONObject();
        o.put("done", true);
        o.put("ok", ok);
        o.put("error", error == null ? "" : error);
        o.put("by", "phone");
        // The completion is also the desktop's durable sent receipt. Without these fields the
        // phone can send successfully while every non-phone client has nothing it can draw.
        o.put("to", to);
        o.put("body", body);
        o.put("at", asked);
        // Keep the desktop's pending MMS intact when this receipt replaces its request event.
        // Without this field absorb() truthfully knew the radio succeeded, but could only replace
        // the pending photo bubble with a text-only one; the encrypted Blossom object then became
        // unreachable from the conversation even though both send and upload had succeeded.
        if (attachment != null) o.put("attachment", attachment);
        byte[] ck = Crypt.conversationKey(sec, me);
        String ct = Crypt.nip44Encrypt(ck, o.toString(), null);

        List<List<String>> tags = new ArrayList<List<String>>();
        List<String> d = new ArrayList<String>(); d.add("d"); d.add(doc); tags.add(d);
        List<String> l = new ArrayList<String>(); l.add("l"); l.add(L_TAG); tags.add(l);

        long now = System.currentTimeMillis() / 1000L;
        String pubHex = hex(me);
        String tagsJson = Nostr.tagsJson(tags);
        String ser = Nostr.serialize(pubHex, now, KIND, tagsJson, ct);
        byte[] id = Nostr.sha256(ser.getBytes("UTF-8"));
        byte[] sig = Nostr.sign(id, sec, null);

        JSONObject out = new JSONObject();
        out.put("id", hex(id));
        out.put("pubkey", pubHex);
        out.put("created_at", now);
        out.put("kind", KIND);
        out.put("tags", new JSONArray(tagsJson));
        out.put("content", ct);
        out.put("sig", hex(sig));
        return out;
    }

    static String hex(byte[] b) {
        StringBuilder s = new StringBuilder(b.length * 2);
        for (byte x : b) s.append(Character.forDigit((x >> 4) & 0xf, 16))
                          .append(Character.forDigit(x & 0xf, 16));
        return s.toString();
    }

    /** How many a single pass will act on, so a caller can stop reading. */
    public static int maxPerPass() { return MAX_PER_PASS; }
}
