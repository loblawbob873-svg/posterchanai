package place.poster.app.sms;

import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.provider.Telephony;
import android.util.Log;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * PICTURE MESSAGES, READ OUT OF THE PHONE'S OWN STORE.
 *
 * `content://mms` is A DIFFERENT TABLE FROM `content://sms`, and almost nothing carries over. It has
 * its own columns, its own message-box constants, the sender in a SECOND table (`/addr`) and the
 * content in a THIRD (`/part`) — and its `date` is in SECONDS where the SMS table's is in
 * milliseconds. Reading it as though it were the SMS table gives timestamps in 1970 and a thread
 * that sorts backwards, which is the shape of every "my pictures are at the top of the list" bug.
 *
 * ONE THING IS SHARED, and it is the thing that makes this work at all: `thread_id`. Both providers
 * write into the same `threads` table, so a conversation containing texts AND pictures has ONE id
 * and the two reads merge on it (see Messages).
 *
 * THIS CLASS ONLY READS. Nothing here writes the provider: PosterChan does not yet FETCH an MMS from
 * the carrier (see MmsDeliverReceiver, which says so out loud rather than filing a placeholder), so
 * what is read here is the history the phone already has — everything received while another app was
 * the default, and everything ever sent. That is the half somebody notices first, and it needs no
 * role, no relay and no network.
 *
 * Like SmsStore, every method answers EMPTY rather than throwing, and `refused()` is what separates
 * "this phone has no picture messages" from "I was not allowed to look". They render identically and
 * only one of them is fixable by the person reading it.
 */
public final class MmsStore {

    private static final String TAG = "PosterChan";

    /**
     * BUILT BY HAND, NOT `Telephony.Mms.Part.CONTENT_URI`.
     *
     * That constant — and `Part.getPartUriForMessage` and `Addr.getAddrUriForMessage` beside it —
     * arrived in API 29. This app's minSdk is 23, where reading them is a NoSuchFieldError at
     * runtime that javac cannot see and the emulator this repo runs will not reproduce. The COLUMN
     * name constants are API 19 and are used freely.
     */
    private static final Uri PART_URI = Uri.parse("content://mms/part");

    /** PduHeaders.FROM — the sender of a received message. */
    private static final int ADDR_FROM = 137;
    /** PduHeaders.TO — the recipient of one we sent. */
    private static final int ADDR_TO = 151;
    /** AOSP files the phone's OWN number under this literal. It is not a person. */
    private static final String SELF = "insert-address-token";

    /**
     * A CEILING ON HOW MANY PICTURE MESSAGES ARE READ AT ONCE, and it is a bound rather than a
     * failure. Each row costs a second query for its sender, so a caller asking for 50,000 (which
     * `loadFromPhone` does, correctly, for texts) would issue fifty thousand cross-process queries
     * on the UI's behalf. Nobody has 2,000 picture messages and a phone that does is not going to
     * render them in a web view.
     */
    private static final int MAX_ROWS = 2000;

    /** How much of a text part is read out of its file before it is treated as prose gone wrong. */
    private static final int MAX_TEXT = 64 * 1024;

    private static final String[] COLS = {
        Telephony.Mms._ID, Telephony.Mms.THREAD_ID, Telephony.Mms.DATE,
        Telephony.Mms.MESSAGE_BOX, Telephony.Mms.READ, Telephony.Mms.SUBJECT,
    };

    private MmsStore() { }

    private static volatile boolean refused = false;

    /** True when the last read could not be performed at all — never the same as "none found". */
    public static boolean refused() { return refused; }

    public static List<SmsMsg> recent(Context ctx, int limit) {
        return query(ctx, null, null, limit);
    }

    /**
     * Everything after a timestamp. THE ARGUMENT IS MILLISECONDS, like every other date in this app,
     * and it is divided here — the column is in seconds. Passing a millisecond value straight into
     * the WHERE clause matches nothing at all until the year 55000, so the archive would simply
     * never publish a picture message and nothing would say why.
     */
    public static List<SmsMsg> since(Context ctx, long dateMs, int limit) {
        return query(ctx, Telephony.Mms.DATE + ">?",
                     new String[]{ String.valueOf(dateMs / 1000L) }, limit);
    }

    public static List<SmsMsg> thread(Context ctx, long threadId, int limit) {
        return thread(ctx, new long[]{ threadId }, limit);
    }

    /** One conversation, which can span several of the platform's thread ids. See SmsStore.thread. */
    public static List<SmsMsg> thread(Context ctx, long[] threadIds, int limit) {
        if (threadIds == null || threadIds.length == 0) return new ArrayList<SmsMsg>();
        return query(ctx, Telephony.Mms.THREAD_ID + " IN (" + SmsStore.marks(threadIds.length) + ")",
                     SmsStore.args(threadIds), limit);
    }

    private static List<SmsMsg> query(Context ctx, String where, String[] args, int limit) {
        List<SmsMsg> out = new ArrayList<SmsMsg>();
        if (ctx == null) return out;
        refused = false;
        int want = Math.max(1, Math.min(limit, MAX_ROWS));
        Cursor c = null;
        try {
            // LIMIT rides on the sort order against a SQLite-backed provider; an OEM provider that
            // rejects it throws the whole query, so the retry asks again without it. Same shape as
            // SmsStore.query, and for the same reason: slower and correct beats fast and empty.
            c = ctx.getContentResolver().query(Telephony.Mms.CONTENT_URI, COLS, where, args,
                                               "date DESC LIMIT " + want);
        } catch (Throwable t) {
            try {
                c = ctx.getContentResolver().query(Telephony.Mms.CONTENT_URI, COLS, where, args,
                                                   "date DESC");
            } catch (Throwable t2) {
                // COULD NOT ASK IS NOT NOTHING THERE — SmsStore's rule, and it matters more here:
                // several OEM providers guard the MMS tables differently from the SMS ones, so this
                // can be refused on a phone where texts read perfectly. Reported separately for
                // exactly that reason.
                refused = true;
                Log.w(TAG, "mms: could not read the picture-message store", t2);
                return out;
            }
        }
        try {
            if (c == null) return out;
            while (c.moveToNext() && out.size() < want) {
                SmsMsg m = new SmsMsg();
                m.mms = true;
                m.id = c.getLong(0);
                m.threadId = c.getLong(1);
                // SECONDS in this table. See the class comment.
                m.date = c.getLong(2) * 1000L;
                m.type = box(c.getInt(3));
                m.read = c.getInt(4) != 0;
                String subject = str(c, 5);
                if (!subject.isEmpty()) m.body = subject;
                out.add(m);
            }
        } catch (Throwable t) {
            Log.w(TAG, "mms: cursor went bad part-way", t);
        } finally {
            if (c != null) try { c.close(); } catch (Throwable ignored) { }
        }
        fillParts(ctx, out);
        fillAddresses(ctx, out);
        return out;
    }

    /**
     * The MMS message boxes, mapped onto the SMS type numbers the rest of the app speaks.
     *
     * AOSP happens to number inbox/sent/drafts/outbox/failed identically in both tables, and this
     * function exists ANYWAY: relying on that coincidence means an OEM that renumbers one of them
     * turns every received picture into a sent one, in the thread, silently. Written out, it is a
     * fact somebody can check.
     */
    private static int box(int msgBox) {
        switch (msgBox) {
            case Telephony.Mms.MESSAGE_BOX_INBOX:  return Telephony.Sms.MESSAGE_TYPE_INBOX;
            case Telephony.Mms.MESSAGE_BOX_SENT:   return Telephony.Sms.MESSAGE_TYPE_SENT;
            case Telephony.Mms.MESSAGE_BOX_DRAFTS: return Telephony.Sms.MESSAGE_TYPE_DRAFT;
            case Telephony.Mms.MESSAGE_BOX_OUTBOX: return Telephony.Sms.MESSAGE_TYPE_OUTBOX;
            case Telephony.Mms.MESSAGE_BOX_FAILED: return Telephony.Sms.MESSAGE_TYPE_FAILED;
            // An unknown box is treated as SENT, not INBOX: claiming somebody received a message
            // they did not is worse than the reverse, and the reverse is what a wrong guess about
            // an outgoing message looks like.
            default: return Telephony.Sms.MESSAGE_TYPE_SENT;
        }
    }

    /**
     * WHAT IS IN THE MESSAGES — in ONE query for the whole batch, not one per message.
     *
     * `content://mms/part` is a flat table keyed by `mid`, so a `mid IN (…)` read answers a hundred
     * messages at once. Per-message it is a hundred cross-process queries to draw one screen, which
     * is the difference between a list that appears and a list that arrives.
     */
    private static void fillParts(Context ctx, List<SmsMsg> rows) {
        if (rows.isEmpty()) return;
        Map<Long, SmsMsg> byId = new LinkedHashMap<Long, SmsMsg>();
        for (SmsMsg m : rows) byId.put(m.id, m);
        Map<Long, StringBuilder> text = new HashMap<Long, StringBuilder>();
        /* IN BATCHES, because the alternative is one `IN (…)` list two thousand terms long. SQLite
         * bounds an expression tree (SQLITE_MAX_EXPR_DEPTH) and the provider gives no way to raise
         * it; over the bound the whole query THROWS, which lands in the catch below and every
         * picture message comes back with no contents at all — a thread of empty bubbles, on the
         * biggest histories only, which is the shape of a bug nobody can reproduce. */
        List<SmsMsg> all = new ArrayList<SmsMsg>(rows);
        for (int from = 0; from < all.size(); from += PART_BATCH) {
            StringBuilder ids = new StringBuilder();
            for (SmsMsg m : all.subList(from, Math.min(from + PART_BATCH, all.size()))) {
                if (ids.length() > 0) ids.append(',');
                ids.append(m.id);
            }
            fillPartBatch(ctx, byId, text, ids.toString());
        }
        for (Map.Entry<Long, StringBuilder> e : text.entrySet()) {
            SmsMsg m = byId.get(e.getKey());
            if (m == null) continue;
            // The SUBJECT is already in `body` when there was one; the text parts go under it.
            m.body = m.body.isEmpty() ? e.getValue().toString()
                                      : m.body + "\n" + e.getValue();
        }
    }

    /** How many message ids go into one `mid IN (…)` read. See fillParts. */
    private static final int PART_BATCH = 200;

    private static final String[] PART_COLS = {
        Telephony.Mms.Part._ID, Telephony.Mms.Part.MSG_ID, Telephony.Mms.Part.CONTENT_TYPE,
        Telephony.Mms.Part.NAME, Telephony.Mms.Part.FILENAME, Telephony.Mms.Part.CONTENT_LOCATION,
        Telephony.Mms.Part.TEXT,
    };

    /** Without the two columns an OEM part table is most likely to be missing. See fillPartBatch. */
    private static final String[] PART_COLS_MIN = {
        Telephony.Mms.Part._ID, Telephony.Mms.Part.MSG_ID, Telephony.Mms.Part.CONTENT_TYPE,
        Telephony.Mms.Part.NAME, Telephony.Mms.Part.TEXT,
    };

    private static int col(Cursor c, String name) {
        try { return c.getColumnIndex(name); } catch (Throwable t) { return -1; }
    }

    private static void fillPartBatch(Context ctx, Map<Long, SmsMsg> byId,
                                      Map<Long, StringBuilder> text, String ids) {
        Cursor c = null;
        try {
            try {
                c = ctx.getContentResolver().query(PART_URI, PART_COLS,
                        Telephony.Mms.Part.MSG_ID + " IN (" + ids + ")", null,
                        Telephony.Mms.Part.MSG_ID + ", " + Telephony.Mms.Part.SEQ);
            } catch (Throwable wide) {
                /* A NARROWER PROJECTION RATHER THAN NOTHING. `fn` and `cl` are the two columns an
                 * OEM part table is most likely not to have, and a projection naming a missing
                 * column throws the WHOLE query — so every picture message would come back with no
                 * contents at all, on that phone only. Losing the best filename costs an attachment
                 * a nicer label; losing the query costs the pictures. */
                c = ctx.getContentResolver().query(PART_URI, PART_COLS_MIN,
                        Telephony.Mms.Part.MSG_ID + " IN (" + ids + ")", null,
                        Telephony.Mms.Part.MSG_ID);
            }
            if (c == null) return;
            // Column indexes are read by NAME here, not by position: the fallback projection is
            // shorter, so a hard-coded index reads the text column as a filename on exactly the
            // phones the fallback exists for.
            final int iId = col(c, Telephony.Mms.Part._ID);
            final int iMid = col(c, Telephony.Mms.Part.MSG_ID);
            final int iCt = col(c, Telephony.Mms.Part.CONTENT_TYPE);
            final int iName = col(c, Telephony.Mms.Part.NAME);
            final int iFn = col(c, Telephony.Mms.Part.FILENAME);
            final int iCl = col(c, Telephony.Mms.Part.CONTENT_LOCATION);
            final int iText = col(c, Telephony.Mms.Part.TEXT);
            while (c.moveToNext()) {
                long mid = iMid < 0 ? 0 : c.getLong(iMid);
                SmsMsg m = byId.get(mid);
                if (m == null) continue;
                SmsPart p = new SmsPart();
                p.id = iId < 0 ? 0 : c.getLong(iId);
                p.ct = str(c, iCt);
                // THREE COLUMNS FOR ONE NAME, and which one is filled depends on the sending phone.
                // Falling back through them is what keeps a photo from being called "attachment" on
                // half the messages — and the name is part of the archive address, so an empty one
                // makes two photos in the same second collide.
                p.name = firstNonEmpty(str(c, iName), str(c, iFn), str(c, iCl));
                if (p.isSmil()) continue;         // the layout, not a thing anybody sent
                if (p.isText()) {
                    String body = str(c, iText);
                    if (body.isEmpty()) body = readText(ctx, p.id);
                    if (!body.isEmpty()) {
                        StringBuilder b = text.get(mid);
                        if (b == null) { b = new StringBuilder(); text.put(mid, b); }
                        if (b.length() > 0) b.append('\n');
                        b.append(body);
                    }
                    continue;                     // the words ARE the body; not an attachment
                }
                p.bytes = sizeOf(ctx, p.id);
                m.parts.add(p);
            }
        } catch (Throwable t) {
            // NOT `refused`. The messages themselves were read; their contents were not, and that is
            // a picture message with an unreadable attachment rather than a store that said no.
            Log.w(TAG, "mms: could not read message parts", t);
        } finally {
            if (c != null) try { c.close(); } catch (Throwable ignored) { }
        }
    }

    /**
     * WHO EACH MESSAGE IS WITH. A second table, one query per message — there is no batch form of
     * `content://mms/<id>/addr` in the platform API, which is why MAX_ROWS exists.
     *
     * A RECEIVED message's person is the FROM address; a SENT one's is the first TO. The phone's own
     * number is filed under a literal placeholder and is skipped, or every conversation is with
     * yourself.
     */
    private static void fillAddresses(Context ctx, List<SmsMsg> rows) {
        for (SmsMsg m : rows) {
            Cursor c = null;
            try {
                c = ctx.getContentResolver().query(
                        Uri.withAppendedPath(Telephony.Mms.CONTENT_URI, m.id + "/addr"),
                        new String[]{ Telephony.Mms.Addr.ADDRESS, Telephony.Mms.Addr.TYPE },
                        null, null, null);
                if (c == null) continue;
                String from = "", to = "", any = "";
                java.util.HashSet<String> people = new java.util.HashSet<String>();
                while (c.moveToNext()) {
                    String a = str(c, 0);
                    if (a.isEmpty() || SELF.equals(a)) continue;
                    people.add(SmsKeys.matchKey(a));
                    int type = 0;
                    try { type = c.getInt(1); } catch (Throwable ignored) { }
                    if (type == ADDR_FROM && from.isEmpty()) from = a;
                    else if (type == ADDR_TO && to.isEmpty()) to = a;
                    if (any.isEmpty()) any = a;
                }
                // Incoming takes FROM; outgoing takes the first TO. `any` is the fallback for a
                // provider that files no type at all — a number with no direction still puts the
                // message in the right conversation, which is what the screen is for.
                m.address = firstNonEmpty(m.incoming() ? from : to, m.incoming() ? any : any, from);
                if (!people.isEmpty()) m.people = people.size();
            } catch (Throwable t) {
                Log.w(TAG, "mms: could not read a message's addresses", t);
            } finally {
                if (c != null) try { c.close(); } catch (Throwable ignored) { }
            }
        }
    }

    /**
     * ONE ATTACHMENT'S BYTES, fetched when something is actually about to show it.
     *
     * `_data` in the part row is a FILE PATH INSIDE THE PROVIDER'S OWN STORAGE and reading it
     * directly fails on every Android since 4.4 — the supported way in is `openInputStream` on
     * `content://mms/part/<id>`, which is what this does.
     *
     * Bounded: a video attachment handed to a WebView as base64 is a third again as large in
     * memory, and an unbounded read on the UI's behalf is an OutOfMemoryError in a messages app.
     * Over the cap it returns null, which the caller reports as "too large to show here" rather
     * than as a missing attachment.
     */
    public static byte[] partBytes(Context ctx, long partId, int maxBytes) {
        InputStream in = null;
        try {
            in = ctx.getContentResolver().openInputStream(
                    Uri.withAppendedPath(PART_URI, String.valueOf(partId)));
            if (in == null) return null;
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[16384];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
                if (out.size() > maxBytes) return null;
            }
            return out.toByteArray();
        } catch (Throwable t) {
            Log.w(TAG, "mms: could not read an attachment", t);
            return null;
        } finally {
            if (in != null) try { in.close(); } catch (Throwable ignored) { }
        }
    }

    /**
     * Is this attachment simply too large to hand through the bridge?
     *
     * Asked only AFTER a fetch came back empty, to tell the two failures apart: an attachment over
     * the cap is a real file somebody can still open in their gallery, and one the provider refused
     * is not. Rendered identically they are both a broken image, which is the drive-check rule again
     * — "could not ask" is never "there is nothing there".
     */
    public static boolean sizeOver(Context ctx, long partId, int maxBytes) {
        long n = sizeOf(ctx, partId);
        return n > maxBytes;
    }

    /** How long a part is, or -1 when the provider would not say — which is never "empty". */
    private static long sizeOf(Context ctx, long partId) {
        android.content.res.AssetFileDescriptor fd = null;
        try {
            fd = ctx.getContentResolver().openAssetFileDescriptor(
                    Uri.withAppendedPath(PART_URI, String.valueOf(partId)), "r");
            if (fd == null) return -1;
            long len = fd.getLength();
            return len < 0 ? -1 : len;
        } catch (Throwable t) {
            return -1;
        } finally {
            if (fd != null) try { fd.close(); } catch (Throwable ignored) { }
        }
    }

    private static String readText(Context ctx, long partId) {
        byte[] b = partBytes(ctx, partId, MAX_TEXT);
        if (b == null) return "";
        try { return new String(b, "UTF-8"); } catch (Throwable t) { return ""; }
    }

    /**
     * Delete picture messages by row id. A DIFFERENT URI FROM A TEXT'S — `content://mms/<id>` — and
     * getting that wrong deletes nothing while reporting nothing, which the client then reads as a
     * provider refusal and correctly leaves the archive alone. So the message stays on the phone AND
     * in the archive, and the delete quietly did not happen.
     */
    public static int delete(Context ctx, long[] ids) {
        if (ids == null || ids.length == 0) return 0;
        int gone = 0;
        for (long id : ids) {
            try {
                gone += ctx.getContentResolver().delete(
                        Uri.withAppendedPath(Telephony.Mms.CONTENT_URI, String.valueOf(id)),
                        null, null);
            } catch (Throwable ignored) { }
        }
        return gone;
    }

    private static String firstNonEmpty(String... values) {
        for (String v : values) if (v != null && !v.isEmpty()) return v;
        return "";
    }

    /** A column's text, or empty — INCLUDING when the projection did not carry it (i < 0). */
    private static String str(Cursor c, int i) {
        if (i < 0) return "";
        try { String s = c.getString(i); return s == null ? "" : s; }
        catch (Throwable t) { return ""; }
    }
}
