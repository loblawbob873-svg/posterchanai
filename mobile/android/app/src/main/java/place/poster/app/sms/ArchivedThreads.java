package place.poster.app.sms;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * WHICH CONVERSATIONS ARE ARCHIVED -- the rule, with no Android in it, so a test can run it.
 *
 * A conversation is archived by one encrypted record per conversation (`pcai:smsarc:<hash>`, written
 * by the Texts screen on any device). The record says "hidden up to the newest message it had"
 * (`upto`, provider milliseconds), never just "hidden": a message dated after that brings the
 * conversation back, on every device, with nothing to publish. An empty record is an unarchive.
 *
 * The same rule is `isArchived` in static/js/client/sms.js, and tests/test_android_sms_archived.py
 * runs the two against each other -- if they disagree, a conversation is filed on the laptop and
 * sitting in the phone's list (or the other way round), which is exactly the inconsistency this
 * exists to remove.
 *
 * Serialised as plain lines (doc TAB key TAB upto TAB createdAt) so it needs no JSON library.
 */
public final class ArchivedThreads {
    private static final class Rec { String key; long upto; long at; }
    private final Map<String, Rec> byDoc = new LinkedHashMap<String, Rec>();

    /** Newest record for a document wins; an older copy arriving late changes nothing. */
    public synchronized boolean put(String doc, String key, long upto, long createdAt) {
        if (doc == null || doc.isEmpty()) return false;
        Rec had = byDoc.get(doc);
        if (had != null && had.at >= createdAt) return false;
        Rec r = new Rec();
        r.key = key == null ? "" : key;
        r.upto = key == null || key.isEmpty() ? 0L : Math.max(0L, upto);
        r.at = createdAt;
        byDoc.put(doc, r);
        return true;
    }

    public synchronized long upto(String conversationKey) {
        long best = 0L;
        for (Rec r : byDoc.values()) if (r.key.equals(conversationKey) && r.upto > best) best = r.upto;
        return best;
    }

    /** Hidden while nothing newer than the archived moment exists in that conversation. */
    public boolean hidden(String address, long newestDate) {
        long u = upto(SmsKeys.matchKey(address));
        return u > 0 && newestDate <= u;
    }

    public synchronized String serialize() {
        StringBuilder b = new StringBuilder();
        for (Map.Entry<String, Rec> e : byDoc.entrySet()) {
            b.append(e.getKey()).append('\t').append(e.getValue().key.replace('\t', ' ').replace('\n', ' '))
             .append('\t').append(e.getValue().upto).append('\t').append(e.getValue().at).append('\n');
        }
        return b.toString();
    }

    public static ArchivedThreads parse(String s) {
        ArchivedThreads t = new ArchivedThreads();
        if (s == null) return t;
        for (String line : s.split("\n")) {
            String[] f = line.split("\t", -1);
            if (f.length < 4) continue;
            try { t.put(f[0], f[1], Long.parseLong(f[2]), Long.parseLong(f[3])); }
            catch (NumberFormatException ignored) { }
        }
        return t;
    }
}
