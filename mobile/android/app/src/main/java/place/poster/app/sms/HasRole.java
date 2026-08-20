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

    /**
     * THE MESSAGES PROVIDER'S OWN ROW, AND ONLY THAT — because it is the one that decides.
     *
     * "posterchan still not working as default Messenger app despite being set as default
     * messenger". `RoleManager.ROLE_SMS` and this legacy row are two different tables on Android
     * 10+, and OEM builds do not always keep them in step. It is tempting to answer yes when either
     * says yes; that would be a claim rather than a measurement. THIS row is what governs whether
     * SMS_DELIVER reaches our receiver and whether a write to `content://sms` is honoured, so an app
     * that answers yes on the strength of the other one would send a message the provider refuses to
     * store and report success — the exact failure `sendingRefusesWhenWeAreNotTheDefaultApp` exists
     * for. The disagreement is REPORTED instead (see roleHeld and SmsPlugin.status), which is what
     * turns "the app says I am not, and I am" into something a person can act on.
     *
     * Never cached. The role can be handed to another app in Settings while this app is running.
     */
    static boolean sms(Context ctx) {
        try {
            String cur = Telephony.Sms.getDefaultSmsPackage(ctx);
            return cur != null && cur.equals(ctx.getPackageName());
        } catch (Throwable t) { return false; }
    }

    /** The RoleManager half, on its own, so the screen can show a disagreement rather than hide it. */
    static boolean roleHeld(Context ctx) {
        try {
            if (android.os.Build.VERSION.SDK_INT < 29) return false;
            android.app.role.RoleManager rm =
                    (android.app.role.RoleManager) ctx.getSystemService(android.content.Context.ROLE_SERVICE);
            return rm != null && rm.isRoleHeld(android.app.role.RoleManager.ROLE_SMS);
        } catch (Throwable t) { return false; }
    }
}
