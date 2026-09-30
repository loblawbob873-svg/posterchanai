package place.poster.app.sms;

import android.content.Context;
import android.content.SharedPreferences;

import org.json.JSONArray;
import org.json.JSONObject;

import place.poster.app.signer.Crypt;
import place.poster.app.signer.Nostr;
import place.poster.app.signer.SignerKey;

/**
 * The phone's copy of "which conversations are archived", fed by the relay service's Texts
 * subscription and read by the native thread list. See ArchivedThreads for the rule.
 *
 * Read-only on this side: archiving and unarchiving happen on the Texts screen (any device). A phone
 * with no key of its own (a remote signer) cannot open the records and simply shows every
 * conversation -- the failure is "nothing hidden", never "something hidden that should not be".
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

    public static ArchivedThreads load(Context ctx) {
        return ArchivedThreads.parse(prefs(ctx).getString("records", ""));
    }

    private static SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }
}
