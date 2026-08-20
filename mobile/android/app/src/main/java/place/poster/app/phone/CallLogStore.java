package place.poster.app.phone;

import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.provider.CallLog;
import android.util.Log;

import java.util.ArrayList;
import java.util.List;

/**
 * THE PHONE'S OWN CALL LOG, which is authoritative and stays that way.
 *
 * The same rule the messages app follows, for the same reason: `CallLog.Calls` is what every other
 * app on the phone reads — the contacts app's "recent", a backup tool, an accessibility reader — and
 * a dialer that keeps its history somewhere private takes it away from all of them.
 *
 * DELIBERATELY NOT MIRRORED TO NOSTR, and that is a decision rather than an omission. The text
 * archive exists because a message can be READ and ANSWERED from a laptop; a call log entry can only
 * be looked at, and the copy would be a second source of truth for something with no second use.
 * When there is a reason — reading missed calls on a desktop — it belongs in the same shape the
 * messages archive already has, and the id rule is already written (SmsKeys).
 *
 * NOTHING HERE WRITES an entry for an ordinary call: the telecom stack logs those itself, and a
 * dialer that also logs them produces every call twice. `logRejected` exists for the one case the
 * platform does not cover on every version.
 */
public final class CallLogStore {

    private static final String TAG = "PosterChan";

    private static final String[] COLS = {
        CallLog.Calls._ID, CallLog.Calls.NUMBER, CallLog.Calls.DATE,
        CallLog.Calls.DURATION, CallLog.Calls.TYPE, CallLog.Calls.CACHED_NAME,
    };

    public static final class Entry {
        public long id;
        public String number = "";
        public String name = "";
        public long date;
        public long durationSec;
        /** CallLog.Calls.*_TYPE: 1 incoming, 2 outgoing, 3 missed, 4 voicemail, 5 rejected, 6 blocked. */
        public int type;

        public boolean missed() { return type == CallLog.Calls.MISSED_TYPE; }
        public boolean outgoing() { return type == CallLog.Calls.OUTGOING_TYPE; }
    }

    private CallLogStore() { }

    public static List<Entry> recent(Context ctx, int limit) {
        return recent(ctx, limit, true);
    }

    /**
     * @param withNames resolve each entry's contact name here rather than in a draw. Always true off
     *                  the UI thread: PhoneLookup is a cross-process query, and a list that asks for
     *                  one per row per repaint stutters exactly where somebody is scrolling.
     */
    public static List<Entry> recent(Context ctx, int limit, boolean withNames) {
        List<Entry> out = new ArrayList<Entry>();
        if (ctx == null) return out;
        Cursor c = null;
        try {
            // LIMIT rides on the sort order against a SQLite-backed provider; an OEM provider that
            // refuses it throws, so the retry asks again without — slower, and correct.
            try {
                c = ctx.getContentResolver().query(CallLog.Calls.CONTENT_URI, COLS, null, null,
                        CallLog.Calls.DATE + " DESC LIMIT " + Math.max(1, limit));
            } catch (Throwable t) {
                c = ctx.getContentResolver().query(CallLog.Calls.CONTENT_URI, COLS, null, null,
                        CallLog.Calls.DATE + " DESC");
            }
            if (c == null) return out;
            while (c.moveToNext() && out.size() < limit) {
                Entry e = new Entry();
                e.id = c.getLong(0);
                e.number = str(c, 1);
                e.date = c.getLong(2);
                e.durationSec = c.getLong(3);
                e.type = c.getInt(4);
                e.name = str(c, 5);
                out.add(e);
            }
        } catch (Throwable t) {
            // No READ_CALL_LOG yet, or a provider that refused. An empty history, never a crash on
            // the screen somebody opened to make a call.
            Log.w(TAG, "tel: could not read the call log", t);
        } finally {
            if (c != null) try { c.close(); } catch (Throwable ignored) { }
        }
        // CACHED_NAME is usually filled in by the platform, but not always — a call from a contact
        // added after the call was logged has none, and that is precisely the row somebody is
        // looking for.
        if (withNames) for (Entry e : out) {
            if (e.name.isEmpty()) e.name = place.poster.app.sms.PhoneBook.nameOf(ctx, e.number);
        }
        return out;
    }

    public static int missedCount(Context ctx) {
        Cursor c = null;
        try {
            c = ctx.getContentResolver().query(CallLog.Calls.CONTENT_URI,
                    new String[]{ CallLog.Calls._ID },
                    CallLog.Calls.TYPE + "=? AND " + CallLog.Calls.NEW + "=1",
                    new String[]{ String.valueOf(CallLog.Calls.MISSED_TYPE) }, null);
            return c == null ? 0 : c.getCount();
        } catch (Throwable t) {
            return 0;
        } finally {
            if (c != null) try { c.close(); } catch (Throwable ignored) { }
        }
    }

    /** Mark missed calls as seen — what opening the recents list means. */
    public static int markSeen(Context ctx) {
        try {
            ContentValues v = new ContentValues();
            v.put(CallLog.Calls.NEW, 0);
            v.put(CallLog.Calls.IS_READ, 1);
            return ctx.getContentResolver().update(CallLog.Calls.CONTENT_URI, v,
                    CallLog.Calls.TYPE + "=? AND " + CallLog.Calls.NEW + "=1",
                    new String[]{ String.valueOf(CallLog.Calls.MISSED_TYPE) });
        } catch (Throwable t) { return 0; }
    }

    public static int delete(Context ctx, long id) {
        try {
            return ctx.getContentResolver().delete(CallLog.Calls.CONTENT_URI,
                    CallLog.Calls._ID + "=?", new String[]{ String.valueOf(id) });
        } catch (Throwable t) { return 0; }
    }

    private static String str(Cursor c, int i) {
        try { String s = c.getString(i); return s == null ? "" : s; }
        catch (Throwable t) { return ""; }
    }
}
