package place.poster.app.sms;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.provider.Telephony;
import android.util.Log;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * THE PHONE'S OWN MESSAGE STORE, WHICH IS AUTHORITATIVE AND STAYS THAT WAY.
 *
 * Only the default messaging app may write `content://sms`, and it MUST: every other app on the
 * phone reads it — the backup tool, the car, the accessibility reader, whatever the person installs
 * next — and a messaging app that keeps texts somewhere private silently takes their messages away
 * from all of it. That is why this class exists and why nothing in PosterChan replaces it.
 *
 * The Nostr archive (static/js/client/sms.js) is a copy ACROSS DEVICES, published from what is read
 * here. When the two disagree, this wins. A delete has to remove both, and the UI says which copies
 * went — see docs/PHONE_SHELL.md.
 *
 * Every method here returns an EMPTY answer rather than throwing. A provider query can fail for
 * reasons that have nothing to do with this app (no permission yet, a locked device, an OEM provider
 * that refuses a column), and a messages app that crashes on a bad cursor is worse than one showing
 * an empty thread with an explanation.
 */
public final class SmsStore {

    private static final String TAG = "PosterChan";

    private static final String[] COLS = {
        Telephony.Sms._ID, Telephony.Sms.THREAD_ID, Telephony.Sms.ADDRESS,
        Telephony.Sms.BODY, Telephony.Sms.DATE, Telephony.Sms.TYPE, Telephony.Sms.READ,
    };

    /** One conversation, as the list screen draws it. */
    public static final class Thread {
        public long id;
        public String address = "";
        public String snippet = "";
        public long date;
        public int unread;
        public int count;
        /**
         * The contact's name, or the number. RESOLVED ON THE BACKGROUND THREAD that built this list
         * (see `threads`), never in a draw: PhoneLookup is a cross-process query and a list screen
         * asks for one per row per repaint — on every keystroke in the search box.
         */
        public String label = "";
        /**
         * EVERY PLATFORM THREAD ID THIS ONE CONVERSATION IS SPREAD ACROSS. Almost always one. See
         * `fold` for why it can be more, and why reading only `id` loses half a conversation.
         */
        public long[] ids = new long[0];
        /** How many people are in it, straight from the provider's recipient list. 1 is ordinary. */
        public int people = 1;
        /** Every participant, for a group. Empty for a one-to-one conversation. */
        public String everyone = "";
    }

    private SmsStore() { }

    /**
     * The newest `limit` messages across every conversation, folded into threads.
     *
     * ONE QUERY, not one per thread. A phone with ten years of texts has tens of thousands of rows
     * and a few dozen conversations, so reading the newest slice and folding it in Java is both
     * faster and simpler than `content://sms/conversations` — whose projection differs between AOSP
     * and several OEM providers, which is the kind of difference that shows up only on somebody
     * else's phone.
     */
    public static List<Thread> threads(Context ctx, int limit) {
        return threads(ctx, limit, true);
    }

    /** @param withNames resolve each conversation's contact name here. Always true off the UI thread. */
    public static List<Thread> threads(Context ctx, int limit, boolean withNames) {
        return fold(ctx, recent(ctx, Math.max(limit, 200)), withNames);
    }

    /**
     * Fold a list of messages into conversations. TAKES THE MESSAGES rather than reading them, so
     * the same rule serves the texts-only read above AND the merged texts-and-pictures read in
     * Messages — a second copy of this fold is a thread list where picture messages have the wrong
     * snippet on one screen and the right one on the other.
     */
    /**
     * THE CONVERSATION LIST — GROUPED BY PERSON, NOT BY THE PLATFORM'S THREAD ID.
     *
     * A thread id is not an identity. Android assigns one from its canonical-addresses table, and
     * the same person reached two ways — "+15551234567" received, "5551234567" dialled — can be
     * given TWO of them. When that happens, grouping by thread id splits one conversation in half
     * and the halves are usually split by DIRECTION, because the incoming format is the carrier's
     * and the outgoing one is whatever dialled it. That is exactly what a phone showed here: two
     * "Mom" rows, and a thread with the other person's messages and none of your own.
     *
     * So the key is the person (SmsKeys.matchKey, the last seven digits — the platform's own rule),
     * and every thread id seen under that key is kept in `ids`, because reading the conversation
     * means reading all of them.
     *
     * GROUPS ARE THE EXCEPTION AND STAY ON THEIR OWN THREAD ID. A group picture message carries a
     * single address like any other (MmsStore.fillAddresses picks one participant), so keying it by
     * that number would fold a conversation several people can read into one member's private
     * thread. `people` is what tells them apart.
     */
    static List<Thread> fold(Context ctx, List<SmsMsg> rows, boolean withNames) {
        Map<String, Thread> byPerson = new LinkedHashMap<String, Thread>();
        Map<String, java.util.LinkedHashSet<Long>> ids =
                new LinkedHashMap<String, java.util.LinkedHashSet<Long>>();
        for (SmsMsg m : rows) {
            String k = groupKey(m);
            Thread t = byPerson.get(k);
            if (t == null) {
                t = new Thread();
                // Rows arrive newest first, so the first one seen carries the conversation's own
                // newest thread id — which is the one a REPLY should join.
                t.id = m.threadId;
                t.address = m.address;
                t.snippet = m.body;
                t.date = m.date;
                byPerson.put(k, t);
                ids.put(k, new java.util.LinkedHashSet<Long>());
            }
            ids.get(k).add(m.threadId);
            t.count++;
            if (m.incoming() && !m.read) t.unread++;
            if (t.address.isEmpty()) t.address = m.address;
        }
        List<Thread> out = new ArrayList<Thread>(byPerson.values());
        for (Map.Entry<String, Thread> e : byPerson.entrySet()) {
            java.util.LinkedHashSet<Long> set = ids.get(e.getKey());
            long[] a = new long[set.size()];
            int i = 0;
            for (Long v : set) a[i++] = v;
            e.getValue().ids = a;
        }
        if (withNames) for (Thread t : out) t.label = PhoneBook.label(ctx, t.address);
        return out;
    }

    /** Which conversation a message belongs to. See fold. */
    private static String groupKey(SmsMsg m) {
        if (m.people > 1) return "t:" + m.threadId;
        String k = m.address == null ? "" : SmsKeys.matchKey(m.address);
        // No usable number (a provider that filed none) can only be identified by its thread id.
        return k.isEmpty() ? "t:" + m.threadId : "p:" + k;
    }

    /**
     * EVERY thread id belonging to one person, for a conversation opened from OUTSIDE our own list
     * -- an `sms:` link, a share sheet, a notification -- where all we are handed is a number.
     *
     * It folds the store rather than asking the platform, because the platform is the thing that
     * split the person in two: `threadIdFor` answers with the id for ONE spelling of the number,
     * which is the id whose half of the conversation you can already see.
     */
    public static long[] idsFor(Context ctx, String address, long fallback) {
        try {
            for (Thread t : Messages.threads(ctx, 500, false)) {
                if (SmsKeys.sameNumber(t.address, address)) return t.ids;
            }
        } catch (Throwable ignored) { }
        return fallback > 0 ? new long[]{ fallback } : new long[0];
    }

    /**
     * THE CONVERSATION LIST, READ FROM THE PLATFORM'S OWN THREADS TABLE.
     *
     * This is what every working messages app does -- checked against Fossify Messages, which is
     * the app that was showing this phone's conversations correctly while ours was not. It reads
     * `content://mms-sms/conversations?simple=true`, whose rows ARE the conversations: the id, the
     * snippet, the date, the message count and the RECIPIENT IDS, maintained by the provider itself
     * across both tables.
     *
     * We used to fold the conversation list out of the messages instead, which is a second opinion
     * about something the platform already knows. Every disagreement between the two shows up as a
     * conversation that is missing, duplicated, or missing half its messages, and none of them can
     * be told apart from the outside. The messages themselves are then read by THREAD_ID exactly as
     * Fossify reads them -- that half was never the difference.
     *
     * Recipient count comes free with the row, so a group is identified by what the provider says
     * rather than inferred from an MMS address table.
     *
     * Returns an EMPTY list when the table cannot be read at all, and the caller falls back to
     * folding. "Could not ask" is not "no conversations".
     */
    public static List<Thread> platformThreads(Context ctx, int limit, boolean withNames) {
        List<Thread> out = new ArrayList<Thread>();
        if (ctx == null) return out;
        Cursor c = null;
        String[] cols = { Telephony.Threads._ID, Telephony.Threads.DATE, Telephony.Threads.SNIPPET,
                          Telephony.Threads.MESSAGE_COUNT, Telephony.Threads.RECIPIENT_IDS,
                          Telephony.Threads.READ };
        try {
            c = ctx.getContentResolver().query(
                    Uri.parse(Telephony.Threads.CONTENT_URI + "?simple=true"), cols,
                    Telephony.Threads.MESSAGE_COUNT + " > 0", null,
                    Telephony.Threads.DATE + " DESC");
        } catch (Throwable t) {
            Log.w(TAG, "sms: could not read the conversation list", t);
            return out;
        }
        java.util.LinkedHashMap<Thread, String> ids = new java.util.LinkedHashMap<Thread, String>();
        try {
            if (c == null) return out;
            while (c.moveToNext() && out.size() < Math.max(1, limit)) {
                Thread t = new Thread();
                t.id = c.getLong(0);
                t.ids = new long[]{ t.id };
                t.date = c.getLong(1);
                t.snippet = str(c, 2);
                t.count = (int) c.getLong(3);
                // The provider's `read` is the whole conversation's. A count of unread messages is
                // not in this table, so an unread conversation counts as one rather than claiming a
                // number it has not looked up.
                t.unread = c.getInt(5) != 0 ? 0 : 1;
                ids.put(t, str(c, 4));
                out.add(t);
            }
        } catch (Throwable t) {
            Log.w(TAG, "sms: the conversation list went bad part-way", t);
        } finally {
            if (c != null) try { c.close(); } catch (Throwable ignored) { }
        }
        fillRecipients(ctx, ids);
        // A group is named after everybody in it. Labelled by `address` alone it takes the name of
        // whichever member the provider happened to list first, which is a conversation several
        // people can read wearing one person's name.
        if (withNames) for (Thread t : out) t.label = groupLabel(ctx, t);
        return out;
    }

    /** Every participant by name, or the one person's. */
    private static String groupLabel(Context ctx, Thread t) {
        if (t.people <= 1 || t.everyone.isEmpty()) return PhoneBook.label(ctx, t.address);
        StringBuilder b = new StringBuilder();
        for (String n : t.everyone.split(", ")) {
            if (b.length() > 0) b.append(", ");
            b.append(PhoneBook.label(ctx, n));
        }
        return b.toString();
    }

    /**
     * RECIPIENT IDS ARE NOT PHONE NUMBERS -- they index `content://mms-sms/canonical-addresses`, and
     * a conversation row carries a space-separated list of them. Resolved in ONE query for the whole
     * screen; per row it is a cross-process query per conversation on every repaint.
     */
    private static void fillRecipients(Context ctx, java.util.LinkedHashMap<Thread, String> ids) {
        if (ids.isEmpty()) return;
        java.util.LinkedHashSet<String> want = new java.util.LinkedHashSet<String>();
        for (String raw : ids.values()) {
            for (String one : raw.trim().split("\\s+")) if (one.matches("\\d+")) want.add(one);
        }
        Map<String, String> number = new java.util.HashMap<String, String>();
        if (!want.isEmpty()) {
            Cursor c = null;
            try {
                StringBuilder in = new StringBuilder();
                for (String one : want) { if (in.length() > 0) in.append(','); in.append(one); }
                c = ctx.getContentResolver().query(
                        Uri.withAppendedPath(Telephony.MmsSms.CONTENT_URI, "canonical-addresses"),
                        new String[]{ "_id", "address" }, "_id IN (" + in + ")", null, null);
                if (c != null) while (c.moveToNext()) number.put(String.valueOf(c.getLong(0)), str(c, 1));
            } catch (Throwable t) {
                Log.w(TAG, "sms: could not resolve who a conversation is with", t);
            } finally {
                if (c != null) try { c.close(); } catch (Throwable ignored) { }
            }
        }
        for (Map.Entry<Thread, String> e : ids.entrySet()) {
            Thread t = e.getKey();
            List<String> people = new ArrayList<String>();
            for (String one : e.getValue().trim().split("\\s+")) {
                String n = number.get(one);
                if (n != null && !n.isEmpty()) people.add(n);
            }
            t.people = people.size();
            if (!people.isEmpty()) t.address = people.get(0);
            if (people.size() > 1) {
                StringBuilder all = new StringBuilder();
                for (String n : people) { if (all.length() > 0) all.append(", "); all.append(n); }
                t.everyone = all.toString();
            }
        }
    }

    /** The newest messages, newest first, across all conversations. */
    public static List<SmsMsg> recent(Context ctx, int limit) {
        return query(ctx, null, null, "date DESC", limit);
    }

    /** One conversation, oldest first — the order a thread is read in. */
    public static List<SmsMsg> thread(Context ctx, long threadId, int limit) {
        return thread(ctx, new long[]{ threadId }, limit);
    }

    /**
     * One conversation, oldest first, across EVERY thread id it is spread over — see fold for why
     * one person can own more than one. Reading a single id is what left a conversation showing the
     * other person's messages and none of your own.
     */
    public static List<SmsMsg> thread(Context ctx, long[] threadIds, int limit) {
        if (threadIds == null || threadIds.length == 0) return new ArrayList<SmsMsg>();
        List<SmsMsg> newest = query(ctx, Telephony.Sms.THREAD_ID + " IN (" + marks(threadIds.length) + ")",
                args(threadIds), "date DESC", limit);
        java.util.Collections.reverse(newest);
        return newest;
    }

    /** `?,?,?` for an IN clause. Shared with MmsStore, which reads the same ids from its own table. */
    static String marks(int n) {
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < n; i++) { if (i > 0) b.append(','); b.append('?'); }
        return b.toString();
    }

    /** Thread ids as selection arguments, to match `marks`. */
    static String[] args(long[] ids) {
        String[] a = new String[ids.length];
        for (int i = 0; i < ids.length; i++) a[i] = String.valueOf(ids[i]);
        return a;
    }

    /**
     * Everything since a timestamp, oldest first — what the archive publishes.
     *
     * STRICTLY AFTER, and the high-water mark the caller keeps is a millisecond date rather than a
     * row id, because a row id is local to one phone: a restored backup renumbers every message and
     * would re-publish the lot.
     */
    public static List<SmsMsg> since(Context ctx, long dateMs, int limit) {
        // OLDEST pending rows, not the newest slice of the backlog. Asking DESC here and reversing
        // afterwards looks oldest-first, but a backlog larger than `limit` has already lost its
        // oldest rows before the reverse. Once the caller advances its cursor those rows can never
        // be asked for again.
        return query(ctx, Telephony.Sms.DATE + ">?",
                new String[]{ String.valueOf(dateMs) }, "date ASC", limit);
    }

    /** Older history, newest page first. Used by the resumable full-history archive backfill. */
    public static List<SmsMsg> before(Context ctx, long dateMs, int limit) {
        return query(ctx, Telephony.Sms.DATE + "<?",
                new String[]{ String.valueOf(Math.max(0L, dateMs)) }, "date DESC", limit);
    }

    /**
     * Whether the LAST read was refused rather than answered empty.
     *
     * Not a count and not a cached list — just the distinction the caller cannot otherwise make.
     * Every read sets it, so it always describes the read that just happened.
     */
    private static volatile boolean refused = false;

    /** True when the last query could not be performed at all (no READ_SMS, provider missing). */
    public static boolean refused() { return refused; }

    private static List<SmsMsg> query(Context ctx, String where, String[] args,
                                      String order, int limit) {
        List<SmsMsg> out = new ArrayList<SmsMsg>();
        if (ctx == null) return out;
        refused = false;
        Cursor c = null;
        try {
            // LIMIT rides on the sort order, which is how it is done against a SQLite-backed
            // provider. If an OEM's provider rejects it the whole query throws, so the retry below
            // asks again without it — slower, and correct, which is the right way round.
            c = ctx.getContentResolver().query(Telephony.Sms.CONTENT_URI, COLS, where, args,
                                               order + " LIMIT " + Math.max(1, limit));
        } catch (Throwable t) {
            try {
                c = ctx.getContentResolver().query(Telephony.Sms.CONTENT_URI, COLS, where, args, order);
            } catch (Throwable t2) {
                // COULD NOT ASK IS NOT NOTHING THERE. Swallowing this returned an empty list, and an
                // empty list is exactly what a phone with no texts returns — so a missing READ_SMS
                // grant drew the same screen as an empty inbox, which is how "i see 0 of my sms
                // messages in Text" survived. The screen asks `refused()` and says which.
                refused = true;
                Log.w(TAG, "sms: could not read the message store", t2);
                return out;
            }
        }
        try {
            if (c == null) return out;
            while (c.moveToNext() && out.size() < limit) {
                SmsMsg m = new SmsMsg();
                m.id = c.getLong(0);
                m.threadId = c.getLong(1);
                m.address = str(c, 2);
                m.body = str(c, 3);
                m.date = c.getLong(4);
                m.type = c.getInt(5);
                m.read = c.getInt(6) != 0;
                out.add(m);
            }
        } catch (Throwable t) {
            Log.w(TAG, "sms: cursor went bad part-way", t);
        } finally {
            if (c != null) try { c.close(); } catch (Throwable ignored) { }
        }
        return out;
    }

    private static String str(Cursor c, int i) {
        try { String s = c.getString(i); return s == null ? "" : s; }
        catch (Throwable t) { return ""; }
    }

    /**
     * File a received message in the phone's inbox. THE ONE THING A DEFAULT SMS APP MUST NOT SKIP —
     * an incoming text that is not written here is gone from every other app on the phone and from
     * every backup, with nothing anywhere to say it ever arrived.
     */
    public static Uri storeInbox(Context ctx, String address, String body, long dateMs,
                                 long dateSentMs) {
        ContentValues v = new ContentValues();
        v.put(Telephony.Sms.ADDRESS, address);
        v.put(Telephony.Sms.BODY, body);
        v.put(Telephony.Sms.DATE, dateMs);
        if (dateSentMs > 0) v.put(Telephony.Sms.DATE_SENT, dateSentMs);
        v.put(Telephony.Sms.READ, 0);
        v.put(Telephony.Sms.SEEN, 0);
        v.put(Telephony.Sms.TYPE, Telephony.Sms.MESSAGE_TYPE_INBOX);
        v.put(Telephony.Sms.THREAD_ID, threadIdFor(ctx, address));
        try {
            return ctx.getContentResolver().insert(Telephony.Sms.CONTENT_URI, v);
        } catch (Throwable t) {
            Log.w(TAG, "sms: could not store an incoming message", t);
            return null;
        }
    }

    /** File a message we sent. Same rule as storeInbox: the phone's own store is the record. */
    public static Uri storeSent(Context ctx, String address, String body, long dateMs, int type) {
        return storeSent(ctx, address, body, dateMs, type, 0);
    }

    /**
     * A REPLY JOINS THE CONVERSATION IT WAS TYPED IN -- pass its thread id, and only fall back to
     * asking the platform when there genuinely is not one (a brand-new conversation).
     *
     * `getOrCreateThreadId` resolves the address through the canonical-addresses table, and the
     * emphasis is on CREATE: handed a spelling that table has not seen -- "5551234567" typed into a
     * conversation the carrier delivers as "+15551234567" -- it does not find the existing thread,
     * it MINTS A NEW ONE. So this app was creating the split it then had to display: every message
     * sent since it became the default landed in a second thread, which is why a conversation showed
     * replies up to the day the phone's messaging app was switched and nothing after it.
     */
    public static Uri storeSent(Context ctx, String address, String body, long dateMs, int type,
                                long threadId) {
        ContentValues v = new ContentValues();
        v.put(Telephony.Sms.ADDRESS, address);
        v.put(Telephony.Sms.BODY, body);
        v.put(Telephony.Sms.DATE, dateMs);
        v.put(Telephony.Sms.READ, 1);
        v.put(Telephony.Sms.SEEN, 1);
        v.put(Telephony.Sms.TYPE, type);
        v.put(Telephony.Sms.THREAD_ID, threadId > 0 ? threadId : threadIdFor(ctx, address));
        try {
            return ctx.getContentResolver().insert(Telephony.Sms.CONTENT_URI, v);
        } catch (Throwable t) {
            Log.w(TAG, "sms: could not store an outgoing message", t);
            return null;
        }
    }

    /** Move a message between states (queued → sent, or → failed) after the radio answers. */
    public static void setType(Context ctx, Uri row, int type) {
        if (row == null) return;
        try {
            ContentValues v = new ContentValues();
            v.put(Telephony.Sms.TYPE, type);
            ctx.getContentResolver().update(row, v, null, null);
        } catch (Throwable ignored) { }
    }

    public static long threadIdFor(Context ctx, String address) {
        try { return Telephony.Threads.getOrCreateThreadId(ctx, address); }
        catch (Throwable t) { return 0; }
    }

    /** Mark a whole conversation read + seen — what opening it means. */
    public static int markRead(Context ctx, long threadId) {
        return markRead(ctx, new long[]{ threadId });
    }

    /**
     * Across every thread id the conversation covers -- one person can own more than one (fold), and
     * marking only the id you opened leaves the other half unread, i.e. a badge nothing can clear.
     */
    public static int markRead(Context ctx, long[] threadIds) {
        if (threadIds == null || threadIds.length == 0) return 0;
        int changed = 0;
        try {
            ContentValues v = new ContentValues();
            v.put(Telephony.Sms.READ, 1);
            v.put(Telephony.Sms.SEEN, 1);
            changed += ctx.getContentResolver().update(Telephony.Sms.CONTENT_URI, v,
                    Telephony.Sms.THREAD_ID + " IN (" + marks(threadIds.length) + ") AND "
                            + Telephony.Sms.READ + "=0",
                    args(threadIds));
        } catch (Throwable ignored) { }
        // A conversation is one surface backed by TWO providers. In particular, group messages are
        // commonly MMS even with no media; leaving that provider unread is the badge that never
        // clears after opening the group.
        changed += MmsStore.markRead(ctx, threadIds);
        return changed;
    }

    /**
     * Delete messages by row id. Returns how many rows really went.
     *
     * A DELETE HERE IS ONLY HALF A DELETE. The other half is the Nostr archive on the person's other
     * devices, and the two must be done together or the next sync puts the message back — the caller
     * (SmsPlugin.delete, and then sms.js) owns that pairing, and the UI says which copies went.
     */
    public static int delete(Context ctx, long[] ids) {
        if (ids == null || ids.length == 0) return 0;
        int gone = 0;
        for (long id : ids) {
            try {
                gone += ctx.getContentResolver().delete(
                        Uri.withAppendedPath(Telephony.Sms.CONTENT_URI, String.valueOf(id)),
                        null, null);
            } catch (Throwable ignored) { }
        }
        return gone;
    }

    /** Delete a whole conversation. */
    public static int deleteThread(Context ctx, long threadId) {
        return deleteThread(ctx, new long[]{ threadId });
    }

    /** Every thread id the conversation covers, or "delete" leaves half of it on the phone. */
    public static int deleteThread(Context ctx, long[] threadIds) {
        if (threadIds == null || threadIds.length == 0) return 0;
        try {
            return ctx.getContentResolver().delete(Telephony.Sms.CONTENT_URI,
                    Telephony.Sms.THREAD_ID + " IN (" + marks(threadIds.length) + ")",
                    args(threadIds));
        } catch (Throwable t) { return 0; }
    }

    public static int unreadCount(Context ctx) {
        Cursor c = null;
        try {
            c = ctx.getContentResolver().query(Telephony.Sms.Inbox.CONTENT_URI,
                    new String[]{ Telephony.Sms._ID }, Telephony.Sms.READ + "=0", null, null);
            return c == null ? 0 : c.getCount();
        } catch (Throwable t) {
            return 0;
        } finally {
            if (c != null) try { c.close(); } catch (Throwable ignored) { }
        }
    }
}
