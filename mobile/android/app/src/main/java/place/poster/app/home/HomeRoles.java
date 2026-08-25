package place.poster.app.home;

import android.app.Activity;
import android.app.role.RoleManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.provider.Settings;

/**
 * THE THREE SYSTEM ROLES — home screen, messages, phone — asked for, checked, and given back.
 *
 * They matter beyond the features they unlock, and the reason is worth stating because it is the
 * principled fix for two long-standing bugs in this app rather than a side effect. Android's
 * restrictions attach to ROLES, not to good behaviour:
 *
 *   * HOME means this process is foreground whenever nothing else is, so the WebView's render
 *     process stops being a low-memory-killer candidate. That killer is the documented cause of
 *     "the APK closes with no error" and of the background folder sweep being throttled.
 *   * SMS and DIALER are documented grounds for a battery-optimisation exemption — a stated
 *     exemption rather than the heuristic one every other app is guessing at.
 *
 * EVERY ONE IS OPT-IN AND EVERY ONE IS GIVEN BACK THE SAME WAY. `createRequestRoleIntent` shows the
 * platform's own dialog; nothing here can grant itself anything.
 *
 * RoleManager is API 29. Below it each role has an older, differently-spelled request, and all three
 * are handled — a phone on Android 6-9 is exactly the phone most likely to be given a second life as
 * somebody's PosterChan handset.
 */
public final class HomeRoles {

    private HomeRoles() { }

    // ------------------------------------------------------------------ HOME

    /**
     * WHY THE COMPONENT SHIPS DISABLED. A CATEGORY_HOME activity makes Android offer this app in the
     * "Select a Home app" chooser from the moment it is installed — including to people who installed
     * a Nostr client and have no idea it can be a launcher, and on OEM builds that pop the chooser on
     * the next HOME press. Disabled by default, enabled at the moment somebody asks, is what makes
     * "opt-in" true rather than nearly true.
     */
    public static void enableLauncherComponent(Context ctx, boolean on) {
        try {
            ctx.getPackageManager().setComponentEnabledSetting(
                    new ComponentName(ctx, HomeActivity.class),
                    on ? PackageManager.COMPONENT_ENABLED_STATE_ENABLED
                       : PackageManager.COMPONENT_ENABLED_STATE_DISABLED,
                    PackageManager.DONT_KILL_APP);
        } catch (Throwable ignored) { }
    }

    public static boolean launcherComponentEnabled(Context ctx) {
        try {
            int s = ctx.getPackageManager().getComponentEnabledSetting(
                    new ComponentName(ctx, HomeActivity.class));
            // A component declared android:enabled="false" reports DEFAULT until something changes
            // it, so DEFAULT must read as OFF here. Reading it as "whatever the manifest says" is how
            // a settings screen ends up claiming the launcher is on when it was never switched on.
            return s == PackageManager.COMPONENT_ENABLED_STATE_ENABLED;
        } catch (Throwable t) { return false; }
    }

    /** Restore the native launcher component after an APK replacement.
     *
     * The manifest keeps HomeActivity disabled so a normal PosterChan install never volunteers to
     * replace somebody's launcher.  Android/OEM package installers can, however, re-apply that
     * manifest default while replacing an APK even when this install was explicitly opted in.  The
     * HOME role is package-scoped on those devices, so Android then falls through to the package's
     * ordinary LAUNCHER activity: pressing Home opens the WebView/mobile app and the native desktop
     * appears to have vanished.  The persisted opt-in is the authority; repairing it is not a new
     * role request and never affects an install that did not opt in. */
    public static void repairOptedInLauncher(Context ctx) {
        try {
            if (new LauncherPrefs(ctx).optedIn() && !launcherComponentEnabled(ctx)) {
                enableLauncherComponent(ctx, true);
            }
        } catch (Throwable ignored) { }
    }

    public static boolean isDefaultHome(Context ctx) {
        try {
            Intent probe = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME);
            android.content.pm.ResolveInfo r = ctx.getPackageManager()
                    .resolveActivity(probe, PackageManager.MATCH_DEFAULT_ONLY);
            return r != null && r.activityInfo != null
                    && ctx.getPackageName().equals(r.activityInfo.packageName);
        } catch (Throwable t) { return false; }
    }

    /**
     * Ask to become the home screen. Enables the component FIRST — the role cannot be requested for a
     * component the platform cannot see, and a request that silently does nothing is the worst
     * possible answer on this particular screen.
     */
    public static Intent requestHome(Context ctx) {
        enableLauncherComponent(ctx, true);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            RoleManager rm = role(ctx);
            if (rm != null && rm.isRoleAvailable(RoleManager.ROLE_HOME)) {
                return rm.createRequestRoleIntent(RoleManager.ROLE_HOME);
            }
        }
        // Below API 29, and on any device whose OEM does not offer the HOME role: the home-app
        // settings screen. It is one tap further away and it is always there.
        return new Intent(Settings.ACTION_HOME_SETTINGS);
    }

    /**
     * STOP BEING THE HOME SCREEN, and the one check that must never be skipped.
     *
     * Disabling our HOME component while we are the ONLY qualifying home app leaves the phone with
     * no home screen at all: HOME does nothing, and there is no way to install one without a home
     * screen to start from. Android will happily let you do it. Returns false, having changed
     * nothing, when that is what was about to happen.
     */
    public static boolean releaseHome(Context ctx) {
        if (!new AppRepo(ctx).anotherHomeExists()) return false;
        enableLauncherComponent(ctx, false);
        return true;
    }

    // ------------------------------------------------------------------ SMS

    public static boolean isDefaultSms(Context ctx) {
        try {
            String cur = android.provider.Telephony.Sms.getDefaultSmsPackage(ctx);
            return cur != null && cur.equals(ctx.getPackageName());
        } catch (Throwable t) { return false; }
    }

    /**
     * CAN THIS BUILD EVEN HOLD THE SMS ROLE?
     *
     * Android refuses the role outright unless the app declares all four of the SMS components, and
     * "refuses" here does not mean an error — `createRequestRoleIntent` returns an Intent, the
     * activity starts, the platform sees the app does not qualify and finishes it immediately with
     * RESULT_CANCELED. From the settings screen that is INDISTINGUISHABLE from a switch that is not
     * wired up: it flips, nothing appears, it flips back. Reported exactly that way.
     *
     * So the switch asks this first and says what it finds, rather than offering a request that
     * cannot succeed. Two of the four are checked because either one missing is fatal and together
     * they cover the receiver half and the service half.
     */
    public static boolean canBeSms(Context ctx) {
        return hasSmsDeliver(ctx) && hasRespondService(ctx);
    }

    /* THE FOUR PARTS ANDROID DEMANDS, ASKED SEPARATELY.
     *
     * "it is impossible to check Messages in User Settings -> Phone, nothing happend" — and the
     * switch's answer for the not-capable case was "update the app and try again", which nobody can
     * act on and which is simply wrong when the build is already current. `canBeSms` collapses the
     * question to a boolean; these say WHICH one is absent, so a dead switch becomes something a
     * person can report and somebody can fix. Two of them are what the role actually turns on
     * (canBeSms above, unchanged); all four are reported. */
    public static boolean hasSmsDeliver(Context ctx) {
        return resolvesBroadcast(ctx, "android.provider.Telephony.SMS_DELIVER");
    }

    public static boolean hasMmsDeliver(Context ctx) {
        return resolvesBroadcast(ctx, "android.provider.Telephony.WAP_PUSH_DELIVER");
    }

    public static boolean hasRespondService(Context ctx) {
        return resolvesService(ctx, "android.intent.action.RESPOND_VIA_MESSAGE");
    }

    public static boolean hasSendTo(Context ctx) {
        try {
            android.content.Intent i = new android.content.Intent(
                    android.content.Intent.ACTION_SENDTO, android.net.Uri.parse("smsto:+15550100"));
            for (android.content.pm.ResolveInfo r
                    : ctx.getPackageManager().queryIntentActivities(i, 0)) {
                if (r.activityInfo != null
                        && ctx.getPackageName().equals(r.activityInfo.packageName)) return true;
            }
        } catch (Throwable ignored) { }
        return false;
    }

    /** Likewise for the dialer: an InCallService that draws the UI, and an ACTION_DIAL activity. */
    public static boolean canBeDialer(Context ctx) {
        return resolvesService(ctx, "android.telecom.InCallService")
            && resolvesActivity(ctx, new Intent(Intent.ACTION_DIAL));
    }

    private static boolean resolvesBroadcast(Context ctx, String action) {
        try {
            for (android.content.pm.ResolveInfo r : ctx.getPackageManager()
                    .queryBroadcastReceivers(new Intent(action), PackageManager.MATCH_ALL)) {
                if (r.activityInfo != null && ctx.getPackageName().equals(r.activityInfo.packageName)) return true;
            }
        } catch (Throwable ignored) { }
        return false;
    }

    private static boolean resolvesService(Context ctx, String action) {
        try {
            for (android.content.pm.ResolveInfo r : ctx.getPackageManager()
                    .queryIntentServices(new Intent(action), PackageManager.MATCH_ALL)) {
                if (r.serviceInfo != null && ctx.getPackageName().equals(r.serviceInfo.packageName)) return true;
            }
        } catch (Throwable ignored) { }
        return false;
    }

    private static boolean resolvesActivity(Context ctx, Intent i) {
        try {
            for (android.content.pm.ResolveInfo r : ctx.getPackageManager()
                    .queryIntentActivities(i, PackageManager.MATCH_ALL)) {
                if (r.activityInfo != null && ctx.getPackageName().equals(r.activityInfo.packageName)) return true;
            }
        } catch (Throwable ignored) { }
        return false;
    }

    /**
     * THE WAY THROUGH WHEN THE ROLE DIALOG DID NOT TAKE. Android's own "Default apps" screen, which
     * exists on every phone and is where an OEM that suppresses the role dialog puts the choice.
     * Offered only AFTER a request came back without the role, so it is a second chance rather than
     * an extra step.
     */
    public static Intent defaultAppsSettings() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            return new Intent(Settings.ACTION_MANAGE_DEFAULT_APPS_SETTINGS);
        }
        return new Intent(Settings.ACTION_SETTINGS);
    }

    public static Intent requestSms(Context ctx) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            RoleManager rm = role(ctx);
            if (rm != null && rm.isRoleAvailable(RoleManager.ROLE_SMS)) {
                return rm.createRequestRoleIntent(RoleManager.ROLE_SMS);
            }
        }
        return new Intent(android.provider.Telephony.Sms.Intents.ACTION_CHANGE_DEFAULT)
                .putExtra(android.provider.Telephony.Sms.Intents.EXTRA_PACKAGE_NAME, ctx.getPackageName());
    }

    // ------------------------------------------------------------------ DIALER

    public static boolean isDefaultDialer(Context ctx) {
        try {
            android.telecom.TelecomManager tm =
                    (android.telecom.TelecomManager) ctx.getSystemService(Context.TELECOM_SERVICE);
            return tm != null && ctx.getPackageName().equals(tm.getDefaultDialerPackage());
        } catch (Throwable t) { return false; }
    }

    public static Intent requestDialer(Context ctx) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            RoleManager rm = role(ctx);
            if (rm != null && rm.isRoleAvailable(RoleManager.ROLE_DIALER)) {
                return rm.createRequestRoleIntent(RoleManager.ROLE_DIALER);
            }
        }
        return new Intent(android.telecom.TelecomManager.ACTION_CHANGE_DEFAULT_DIALER)
                .putExtra(android.telecom.TelecomManager.EXTRA_CHANGE_DEFAULT_DIALER_PACKAGE_NAME,
                          ctx.getPackageName());
    }

    // ------------------------------------------------------------------ battery

    /**
     * Is this app exempt from Doze? Not asked for anywhere here — SignerPlugin already owns the
     * request — but the SMS and dialer roles are the documented GROUNDS for one, and the settings
     * screen says so rather than leaving the person to guess why it now sticks.
     */
    public static boolean batteryExempt(Context ctx) {
        try {
            android.os.PowerManager pm =
                    (android.os.PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
            return Build.VERSION.SDK_INT < Build.VERSION_CODES.M
                    || (pm != null && pm.isIgnoringBatteryOptimizations(ctx.getPackageName()));
        } catch (Throwable t) { return false; }
    }

    private static RoleManager role(Context ctx) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q) return null;
        try { return (RoleManager) ctx.getSystemService(Context.ROLE_SERVICE); }
        catch (Throwable t) { return null; }
    }

    /** Start a role request from an activity, returning false when the platform offered no route. */
    public static boolean ask(Activity a, Intent request, int requestCode) {
        if (a == null || request == null) return false;
        try { a.startActivityForResult(request, requestCode); return true; }
        catch (Throwable t) { return false; }
    }
}
