package place.poster.app.sms;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import place.poster.app.signer.Crypt;
import place.poster.app.signer.Nostr;
import place.poster.app.signer.SignerRelayService;
import place.poster.app.signer.SignerKey;

/**
 * The phone's copy of "which conversations are archived", fed by the relay service's Texts
 * subscription and read by the native thread list. See ArchivedThreads for the rule.
 *
 * The phone archives too (`set`, from the long-press menu of the native list): it writes the SAME
 * record the web Texts screen writes, so either side can undo the other. A phone with no key of its
 * own (a remote signer) cannot open the records and simply shows every conversation -- the failure is
 * "nothing hidden", never "something hidden that should not be" -- and cannot write one either, which
 * `set` says rather than pretending.
 *
 * A record written here is held in a durable queue until a relay ACCEPTS it (an OK, not merely a
 * socket write): the native list has no WebView behind it and is mostly used with the app closed, so
 * "sent into a socket that was closing" must not be where an archive quietly ends.
 */
public final class SmsArchived {
    public static final String D_ARC = "pcai:smsarc:";
    public static final String L_ARC = "pcai-smsarc";
    private static final String PREFS = "pc_sms_archived";
    private static final Object LOCK = new Object();

    private SmsArchived() { }

    /** Every archive record this account has, on its own index tag (the Texts tag holds thousands). */
    public static JSONObject filter(String mePubHex) throws Exception {
        JSONObject f = new JSONObject();
        f.put("kinds", new JSONArray().put(30078));
        f.put("authors", new JSONArray().put(mePubHex));
        f.put("#l", new JSONArray().put(L_ARC));
        f.put("limit", 5000);
        return f;
    }

    public static String docOf(JSONObject ev) {
        JSONArray tags = ev.optJSONArray("tags");
        if (tags == null) return "";
        for (int i = 0; i < tags.length(); i++) {
            JSONArray t = tags.optJSONArray(i);
            if (t != null && t.length() >= 2 && "d".equals(t.optString(0))) return t.optString(1);
        }
        return "";
    }

    /** Take one event if it is an archive record. Returns true when the stored state changed. */
    public static boolean absorb(Context ctx, JSONObject ev) {
        try {
            if (ev == null) return false;
            String doc = docOf(ev);
            if (!doc.startsWith(D_ARC)) return false;
            byte[] sec = SignerKey.load(ctx);
            if (sec == null) return false;
            byte[] me = Nostr.pubkey(sec);
            if (!Nostr.hex(me).equals(ev.optString("pubkey", ""))) return false;
            String key = "";
            long upto = 0L;
            String content = ev.optString("content", "");
            if (!content.isEmpty()) {
                JSONObject rec = new JSONObject(Crypt.nip44Decrypt(Crypt.conversationKey(sec, me), content));
                key = rec.optString("k", "");
                upto = rec.optLong("upto", 0L);
            }
            synchronized (LOCK) {
                ArchivedThreads t = load(ctx);
                if (!t.put(doc, key, upto, ev.optLong("created_at", 0L))) return false;
                prefs(ctx).edit().putString("records", t.serialize()).apply();
                return true;
            }
        } catch (Throwable ignored) { return false; }
    }

    /** What `set` did, for the screen to say. */
    public static final int SENT = 0, QUEUED = 1, NO_KEY = -1, FAILED = -2;

    /**
     * Archive (on) or unarchive (off) one conversation, from this phone.
     *
     * The record is applied here at once -- it is the very event the relay will hold, so this phone
     * can never believe something the other devices will not be told -- and queued for the relay
     * service. `newestDate` is the conversation's newest message: the archive covers up to it, and
     * anything newer brings the conversation back on every device.
     */
    public static int set(Context ctx, String address, long newestDate, boolean on) {
        try {
            byte[] sec = SignerKey.load(ctx);
            if (sec == null) return NO_KEY;
            byte[] me = Nostr.pubkey(sec);
            String meHex = Nostr.hex(me);
            String key = SmsKeys.convKey(address);
            if (key.isEmpty()) return FAILED;
            String doc = ArchivedThreads.docFor(meHex, key);
            if (doc.isEmpty()) return FAILED;
            JSONObject ev;
            synchronized (LOCK) {
                long at = load(ctx).nextAt(doc, System.currentTimeMillis() / 1000L);
                String content = "";
                if (on) {
                    JSONObject rec = new JSONObject();
                    rec.put("k", key);
                    rec.put("upto", Math.max(1L, newestDate));
                    rec.put("at", System.currentTimeMillis());
                    content = Crypt.nip44Encrypt(Crypt.conversationKey(sec, me), rec.toString(), null);
                }
                java.util.List<java.util.List<String>> tags = new java.util.ArrayList<>();
                tags.add(java.util.Arrays.asList("d", doc));
                tags.add(java.util.Arrays.asList("l", "pcai-sms"));
                tags.add(java.util.Arrays.asList("l", L_ARC));
                ev = SmsOutbox.signed(sec, meHex, at, 30078, tags, content);
                // Newer replaces older for the same conversation: only the last word needs to go out.
                pending(ctx).edit().putString(doc, ev.toString()).commit();
            }
            if (!absorb(ctx, ev)) return FAILED;
            return SignerRelayService.sendArchived(ctx) ? SENT : QUEUED;
        } catch (Throwable t) { return FAILED; }
    }

    /** Every record still waiting for a relay to accept it. */
    public static java.util.List<JSONObject> unsent(Context ctx) {
        java.util.List<JSONObject> out = new java.util.ArrayList<>();
        for (Object v : pending(ctx).getAll().values()) {
            if (!(v instanceof String)) continue;
            try { out.add(new JSONObject((String) v)); } catch (Throwable ignored) { }
        }
        return out;
    }

    /** A relay said OK to this event id: it no longer needs sending. A newer record stays queued. */
    public static void accepted(Context ctx, String id) {
        if (id == null || id.isEmpty()) return;
        synchronized (LOCK) {
            SharedPreferences p = pending(ctx);
            for (java.util.Map.Entry<String, ?> e : p.getAll().entrySet()) {
                Object v = e.getValue();
                if (v instanceof String && ((String) v).contains(id)) {
                    try {
                        if (id.equals(new JSONObject((String) v).optString("id", "")))
                            p.edit().remove(e.getKey()).apply();
                    } catch (Throwable bad) { p.edit().remove(e.getKey()).apply(); }
                }
            }
        }
    }

    private static SharedPreferences pending(Context ctx) {
        return ctx.getSharedPreferences(PREFS + "_unsent", Context.MODE_PRIVATE);
    }

    public static ArchivedThreads load(Context ctx) {
        return ArchivedThreads.parse(prefs(ctx).getString("records", ""));
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }
}
