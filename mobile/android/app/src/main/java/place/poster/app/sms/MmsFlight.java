package place.poster.app.sms;

import android.content.Context;

/** One system MMS transaction at a time: the bundled transport reuses one PendingIntent identity. */
final class MmsFlight {
    private static final String PREF = "poster_mms_flight";
    private static final String AT = "at";
    private static final long STALE_MS = 3 * 60 * 1000L;
    private MmsFlight() { }

    static synchronized boolean claim(Context ctx) {
        long now = System.currentTimeMillis();
        long at = ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE).getLong(AT, 0);
        if (at > 0 && now - at < STALE_MS) return false;
        ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE).edit().putLong(AT, now).commit();
        return true;
    }

    static synchronized void release(Context ctx) {
        if (ctx != null) ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE).edit().remove(AT).apply();
    }
}
