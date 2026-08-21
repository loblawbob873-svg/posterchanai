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
    /**
     * ARE WE THIS PHONE'S MESSAGES APP — and the message store's row is not the only way to be.
     *
     * This asked `getDefaultSmsPackage` alone. On a real device it answered:
     *
     *     role: true    store names: (none)
     *
     * Android had granted PosterChan the SMS ROLE and the store's default-app row was EMPTY. The two
     * are separate tables on Android 10+ and OEM builds do not always write both. So the app said
     * "PosterChan is not this phone's messages app" to somebody who had just made it exactly that,
     * every gate that depended on it did nothing, and setting the default again changed nothing
     * visible — "the checkbox in settings never works", all day.
     *
     * NOBODY NAMED PLUS THE ROLE MEANS US. There is no other candidate: the role is granted to one
     * app, and the row that would name a different one is empty.
     *
     * ANOTHER APP NAMED STILL WINS, even if we hold the role. That is the case where messages are
     * genuinely being delivered elsewhere, and claiming otherwise would have us write into a store
     * somebody else owns and report sends that another app performed.
     */
    static boolean sms(Context ctx) {
        try {
            String mine = ctx.getPackageName();
            String cur = Telephony.Sms.getDefaultSmsPackage(ctx);
            if (cur != null && !cur.isEmpty()) return cur.equals(mine);
            return roleHeld(ctx);
        } catch (Throwable t) { return false; }
    }

    /**
     * CAN THIS DEVICE DO SMS AT ALL — and `FEATURE_TELEPHONY` is NOT that question.
     *
     * "Still Android has not named a messages app for this phone yet", again, on a build that
     * already checked for telephony. `hasSystemFeature(FEATURE_TELEPHONY)` is true on plenty of
     * Wi-Fi-only tablets: they ship the telephony stack, they simply have no radio to send a message
     * with. So the check passed, the no-SIM branch was skipped, and the screen went back to telling
     * a tablet to go and choose a messages app.
     *
     * `TelephonyManager.isSmsCapable()` is the question actually being asked — the platform's own
     * "this device can send and receive text messages" — and it is what a Wi-Fi tablet answers false
     * to. `FEATURE_TELEPHONY_MESSAGING` (API 31) says the same thing a different way and is checked
     * beside it, because an OEM that gets one wrong rarely gets both wrong. FEATURE_TELEPHONY stays
     * only as the last resort for API levels that have neither.
     *
     * Three signals, all reported by SmsPlugin.diagnose, so the next time this is wrong the phone
     * can say WHICH of them lied instead of me guessing at it a third time.
     */
    static boolean smsCapable(Context ctx) {
        try {
            android.telephony.TelephonyManager tm = (android.telephony.TelephonyManager)
                    ctx.getSystemService(Context.TELEPHONY_SERVICE);
            if (tm != null && tm.isSmsCapable()) return true;
        } catch (Throwable ignored) { }
        try {
            if (android.os.Build.VERSION.SDK_INT >= 31
                    && ctx.getPackageManager().hasSystemFeature(
                           android.content.pm.PackageManager.FEATURE_TELEPHONY_MESSAGING)) return true;
            // Below 31 there is nothing better than the coarse feature flag; above it, a device that
            // said no to BOTH of the precise questions is taken at its word rather than being
            // overruled by the flag that is true on every tablet.
            if (android.os.Build.VERSION.SDK_INT < 31
                    && ctx.getPackageManager().hasSystemFeature(
                           android.content.pm.PackageManager.FEATURE_TELEPHONY)) return true;
        } catch (Throwable ignored) { }
        return false;
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
