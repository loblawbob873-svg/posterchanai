package place.poster.app.phone;

import android.content.Context;
import android.telecom.TelecomManager;

/**
 * ARE WE THIS PHONE'S PHONE APP — asked here rather than imported from the launcher package.
 *
 * One line, deliberately duplicated (the messages half has its own, HasRole): each feature must
 * still work in a build where the others are absent. Nobody remembers the answer, because the role
 * can be handed to another app in Settings while this screen is open, and a cached "yes" is how a
 * dialer ends up telling somebody a call was placed that nothing placed.
 */
final class HasDialerRole {
    private HasDialerRole() { }

    static boolean yes(Context ctx) {
        try {
            TelecomManager tm = (TelecomManager) ctx.getSystemService(Context.TELECOM_SERVICE);
            return tm != null && ctx.getPackageName().equals(tm.getDefaultDialerPackage());
        } catch (Throwable t) { return false; }
    }
}
