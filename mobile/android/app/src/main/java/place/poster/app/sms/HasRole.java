package place.poster.app.sms;

import android.content.Context;
import android.provider.Telephony;

/**
 * ARE WE THIS PHONE'S MESSAGES APP — asked here rather than imported from the launcher package.
 *
 * One line, deliberately duplicated: HomeRoles is the launcher's, and the SMS half must still work
 * in a build where the launcher does not exist. Both ask the platform; neither remembers an answer,
 * because the role can be taken away in Settings while this app is running and a cached "yes" is how
 * an app ends up writing to a provider it no longer owns and reporting success.
 */
final class HasRole {
    private HasRole() { }

    static boolean sms(Context ctx) {
        try {
            String cur = Telephony.Sms.getDefaultSmsPackage(ctx);
            return cur != null && cur.equals(ctx.getPackageName());
        } catch (Throwable t) { return false; }
    }
}
