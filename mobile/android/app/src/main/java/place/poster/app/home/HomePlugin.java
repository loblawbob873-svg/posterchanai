package place.poster.app.home;

import android.content.Intent;

import androidx.activity.result.ActivityResult;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.ActivityCallback;
import com.getcapacitor.annotation.CapacitorPlugin;

import place.poster.app.ui.PcTheme;
import place.poster.app.ui.PcThemeStore;

/**
 * THE SETTINGS SCREEN'S DOOR TO THE THREE SYSTEM ROLES — home screen, messages, phone.
 *
 * Everything here is a request the PLATFORM answers. Nothing in this plugin can grant itself a role;
 * `createRequestRoleIntent` shows Android's own dialog and the answer comes back through
 * `@ActivityCallback`. The switches in the app read `status()` and never assume — a role can be taken
 * away in Settings while the app is running, so the only honest source is a fresh check.
 *
 * It also carries the THEME across the boundary. The launcher, the dialer and the SMS screens have no
 * WebView by design (that is what makes them survive a dead renderer) and therefore cannot read
 * localStorage, so `setTheme` mirrors the client's `pc_theme` into SharedPreferences where a plain
 * Activity can find it. localStorage stays authoritative; this is a copy, written only from it.
 */
@CapacitorPlugin(name = "HomeScreen")
public class HomePlugin extends Plugin {

    @PluginMethod
    public void status(PluginCall call) {
        JSObject o = new JSObject();
        o.put("sdk", android.os.Build.VERSION.SDK_INT);
        o.put("launcherEnabled", HomeRoles.launcherComponentEnabled(getContext()));
        o.put("isDefaultHome", HomeRoles.isDefaultHome(getContext()));
        o.put("isDefaultSms", HomeRoles.isDefaultSms(getContext()));
        o.put("isDefaultDialer", HomeRoles.isDefaultDialer(getContext()));
        o.put("batteryExempt", HomeRoles.batteryExempt(getContext()));
        // Whether there is anything to hand the home screen BACK to. A switch that would leave the
        // phone with no home screen at all must be able to say so before it is flipped, not after.
        o.put("anotherHome", new AppRepo(getContext()).anotherHomeExists());
        o.put("theme", PcThemeStore.slug(getContext()));
        /* Whether the person ever ASKED for the launcher, as opposed to whether they currently hold
           the role. The two differ when another app has since been made the home screen, and the
           settings screen needs to tell those apart: "you turned this on and something else took it"
           is a different sentence from "you never turned it on". */
        o.put("optedIn", new LauncherPrefs(getContext()).optedIn());
        /* WHETHER THIS BUILD CAN HOLD EACH ROLE AT ALL. Android refuses the SMS role unless the app
           declares all four of its components, and refuses SILENTLY — the request activity starts and
           finishes with RESULT_CANCELED, which on the settings screen looks exactly like a switch
           that is not wired up. Reported as "sms does nothing when checked". The switch reads this
           and explains, rather than offering a request that cannot succeed. */
        o.put("smsCapable", HomeRoles.canBeSms(getContext()));
        o.put("dialerCapable", HomeRoles.canBeDialer(getContext()));
        call.resolve(o);
    }

    @PluginMethod
    public void enableLauncher(PluginCall call) {
        try {
            Intent i = HomeRoles.requestHome(getContext());
            new LauncherPrefs(getContext()).setOptedIn(true);
            asking = "home";
            startActivityForResult(call, i, "roleResult");
        } catch (Throwable t) {
            call.reject("could not ask to be the home screen: " + t);
        }
    }

    /**
     * GIVE THE HOME SCREEN BACK. Refuses — having changed nothing — when this app is the only home
     * app on the phone, because disabling the component then leaves the device with no home screen
     * and no way to install one. Android will let you do it; this will not.
     */
    @PluginMethod
    public void disableLauncher(PluginCall call) {
        boolean ok = HomeRoles.releaseHome(getContext());
        if (ok) new LauncherPrefs(getContext()).setOptedIn(false);
        JSObject o = new JSObject();
        o.put("released", ok);
        if (!ok) o.put("reason", "no other home app is installed on this phone");
        call.resolve(o);
    }

    @PluginMethod
    public void requestSms(PluginCall call) {
        asking = "sms";
        try { startActivityForResult(call, HomeRoles.requestSms(getContext()), "roleResult"); }
        catch (Throwable t) { call.reject("could not ask to be the messages app: " + t); }
    }

    @PluginMethod
    public void requestDialer(PluginCall call) {
        asking = "dialer";
        try { startActivityForResult(call, HomeRoles.requestDialer(getContext()), "roleResult"); }
        catch (Throwable t) { call.reject("could not ask to be the phone app: " + t); }
    }

    /**
     * The result code is deliberately IGNORED and the state is re-measured instead. RoleManager
     * answers RESULT_OK for "the user pressed yes", but a phone can also grant the role by another
     * route mid-dialog, an OEM can substitute its own picker with its own result convention, and
     * ACTION_CHANGE_DEFAULT (the pre-29 path) returns nothing meaningful at all. Asking the platform
     * what is true now is the only answer that is true on every phone.
     *
     * BUT NOT IMMEDIATELY, AND THAT IS THE WHOLE OF THE SECOND BUG. Granting a role is asynchronous
     * on the system side: the dialog returns, and for a moment `getDefaultSmsPackage` still names the
     * OLD app. Read once, right there, and the answer is "no" for a role that was in fact granted —
     * the switch springs back while Android's own settings screen already says PosterChan. Reported
     * exactly that way: "i check the box to make it my sms app, it unchecks, android says my default
     * app is posterchan."
     *
     * So it is re-read until it settles, or for a second and a half, whichever comes first. Polling
     * is normally what this codebase refuses to do; here it is bounded, it happens once per press,
     * and the alternative is telling somebody the opposite of what their phone says.
     */
    @ActivityCallback
    private void roleResult(final PluginCall call, ActivityResult result) {
        if (call == null) return;
        settle(call, 0);
    }

    private static final int SETTLE_TRIES = 6;
    private static final int SETTLE_STEP_MS = 250;

    /**
     * WHICH role the outstanding request was for. Settling on "any role is held" would return
     * instantly for somebody who already has the home screen and is now granting SMS — which is the
     * same bug wearing a different hat, and the harder one to spot because it only happens to people
     * who have already opted into something.
     */
    private String asking = "";

    private boolean asked() {
        if ("sms".equals(asking)) return HomeRoles.isDefaultSms(getContext());
        if ("dialer".equals(asking)) return HomeRoles.isDefaultDialer(getContext());
        if ("home".equals(asking)) return HomeRoles.isDefaultHome(getContext());
        return true;                       // nothing outstanding: answer with what is true now
    }

    private void settle(final PluginCall call, final int tries) {
        if (asked() || tries >= SETTLE_TRIES) { asking = ""; status(call); return; }
        new android.os.Handler(android.os.Looper.getMainLooper()).postDelayed(new Runnable() {
            @Override public void run() { settle(call, tries + 1); }
        }, SETTLE_STEP_MS);
    }

    /**
     * Android's own "Default apps" screen. The settings card offers it only after a role request
     * came back without the role — on an OEM build that suppresses the role dialog it is the only
     * route, and without it the switch is a dead end that never says so.
     */
    @PluginMethod
    public void openDefaultApps(PluginCall call) {
        try {
            getContext().startActivity(HomeRoles.defaultAppsSettings()
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            call.resolve();
        } catch (Throwable t) {
            call.reject("this phone has no default-apps screen: " + t);
        }
    }

    /** Mirror the client's theme where the native screens can read it. Unknown slugs are ignored. */
    @PluginMethod
    public void setTheme(PluginCall call) {
        String slug = call.getString("slug", "");
        PcThemeStore.remember(getContext(), slug);
        JSObject o = new JSObject();
        o.put("theme", PcThemeStore.slug(getContext()));
        o.put("known", PcTheme.known(slug));
        call.resolve(o);
    }

    /**
     * WHICH SCREEN A HOME-SCREEN TILE ASKED FOR, read once and then cleared.
     *
     * The same shape as MusicPlugin.consumeLaunchAction, and for the same reason: a launch extra
     * lives on the Activity's intent for as long as that intent does, so without consuming it the
     * app would jump to Notes again on every later resume. The timestamp is what makes "consume"
     * safe across a process restart — an extra older than a minute is a stale intent being replayed,
     * not somebody pressing a tile.
     */
    @PluginMethod
    public void consumeLaunchView(PluginCall call) {
        JSObject o = new JSObject();
        o.put("view", "");
        // THE PARKED REQUEST IS READ FIRST, and the intent extra second. They cover disjoint halves:
        // a warm start is the case where the extra is dropped, a cold start is the case where there
        // is no process to have parked anything. Whichever answers wins, and BOTH are cleared either
        // way — leaving one behind is how a press gets re-performed on a later resume.
        String parked = "";
        try { parked = LaunchView.take(System.currentTimeMillis()); } catch (Throwable ignored) { }
        try {
            android.app.Activity a = getActivity();
            Intent i = a == null ? null : a.getIntent();
            if (i != null) {
                String v = i.getStringExtra(HomeActivity.EXTRA_VIEW);
                long at = i.getLongExtra(HomeActivity.EXTRA_VIEW_AT, 0);
                if (parked.isEmpty() && v != null && !v.isEmpty()
                        && System.currentTimeMillis() - at < LaunchView.MAX_AGE_MS) {
                    parked = v;
                }
                i.removeExtra(HomeActivity.EXTRA_VIEW);
                i.removeExtra(HomeActivity.EXTRA_VIEW_AT);
            }
        } catch (Throwable ignored) { }
        o.put("view", parked);
        call.resolve(o);
    }
}
