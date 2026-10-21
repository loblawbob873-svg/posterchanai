package place.poster.app.phone;

import android.content.ContentValues;
import android.content.Context;
import android.provider.BlockedNumberContract;

/**
 * BLOCKING A CALLER OR A TEXTER -- the phone's own blocked-numbers list, shared by Phone and Texts.
 *
 * Reported: "Phone App -> No way to block callers" and "Texts App -> No way to block texters". Both are
 * ONE list on Android (BlockedNumberContract), and it is the platform that enforces it: Telecom refuses
 * a blocked caller before our InCallService ever hears of the call, and the telephony stack drops a
 * blocked number's SMS/MMS before SMS_DELIVER. So this class only reads and writes the list; nothing
 * here has to screen anything, and a number blocked here is blocked for the stock apps too.
 *
 * Only the DEFAULT dialer or SMS app may touch it (canCurrentUserBlockNumbers), which this app is when
 * it is doing either job. "Could not ask" is answered as null, never as "not blocked": a menu then
 * offers neither Block nor Unblock rather than a choice that is a lie. Every write is READ BACK -- the
 * provider can refuse quietly -- and the caller is told what the list now says.
 */
public final class Blocklist {
    private Blocklist() { }

    /** A single number somebody can block: not empty, and not a group conversation ("a;b", "a,b"). */
    public static boolean blockable(String number) {
        if (number == null) return false;
        String n = number.trim();
        return !n.isEmpty() && n.indexOf(';') < 0 && n.indexOf(',') < 0 && n.replaceAll("[^0-9A-Za-z]", "").length() > 0;
    }

    public static boolean available(Context ctx) {
        try { return BlockedNumberContract.canCurrentUserBlockNumbers(ctx); } catch (Throwable t) { return false; }
    }

    /** true / false, or null when the list cannot be asked (not the default app, a provider error). */
    public static Boolean isBlocked(Context ctx, String number) {
        if (!blockable(number) || !available(ctx)) return null;
        try { return BlockedNumberContract.isBlocked(ctx, number.trim()); } catch (Throwable t) { return null; }
    }

    /** Sets the number blocked or not; answers what the list says AFTERWARDS (null = could not ask). */
    public static Boolean set(Context ctx, String number, boolean block) {
        if (!blockable(number) || !available(ctx)) return null;
        String n = number.trim();
        try {
            if (block) {
                ContentValues v = new ContentValues();
                v.put(BlockedNumberContract.BlockedNumbers.COLUMN_ORIGINAL_NUMBER, n);
                ctx.getContentResolver().insert(BlockedNumberContract.BlockedNumbers.CONTENT_URI, v);
            } else {
                BlockedNumberContract.unblock(ctx, n);
            }
        } catch (Throwable ignored) { }
        return isBlocked(ctx, n);
    }

    /** What to tell the person after set(): the outcome as measured, never as intended. */
    public static int outcome(boolean wanted, Boolean now) {
        if (now == null) return UNKNOWN;
        if (now == wanted) return wanted ? BLOCKED : UNBLOCKED;
        return REFUSED;
    }
    public static final int BLOCKED = 0, UNBLOCKED = 1, REFUSED = 2, UNKNOWN = 3;
}
