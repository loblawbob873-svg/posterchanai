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
        call.resolve(o);
    }

    @PluginMethod
    public void enableLauncher(PluginCall call) {
        try {
            Intent i = HomeRoles.requestHome(getContext());
            new LauncherPrefs(getContext()).setOptedIn(true);
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
        try { startActivityForResult(call, HomeRoles.requestSms(getContext()), "roleResult"); }
        catch (Throwable t) { call.reject("could not ask to be the messages app: " + t); }
    }

    @PluginMethod
    public void requestDialer(PluginCall call) {
        try { startActivityForResult(call, HomeRoles.requestDialer(getContext()), "roleResult"); }
        catch (Throwable t) { call.reject("could not ask to be the phone app: " + t); }
    }

    /**
     * The result code is deliberately IGNORED and the state is re-measured instead. RoleManager
     * answers RESULT_OK for "the user pressed yes", but a phone can also grant the role by another
     * route mid-dialog, an OEM can substitute its own picker with its own result convention, and
     * ACTION_CHANGE_DEFAULT (the pre-29 path) returns nothing meaningful at all. Asking the platform
     * what is true now is the only answer that is true on every phone.
     */
    @ActivityCallback
    private void roleResult(PluginCall call, ActivityResult result) {
        if (call == null) return;
        status(call);
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
        try {
            android.app.Activity a = getActivity();
            Intent i = a == null ? null : a.getIntent();
            if (i != null) {
                String v = i.getStringExtra(HomeActivity.EXTRA_VIEW);
                long at = i.getLongExtra(HomeActivity.EXTRA_VIEW_AT, 0);
                if (v != null && !v.isEmpty() && System.currentTimeMillis() - at < 60000) {
                    o.put("view", v);
                }
                i.removeExtra(HomeActivity.EXTRA_VIEW);
                i.removeExtra(HomeActivity.EXTRA_VIEW_AT);
            }
        } catch (Throwable ignored) { }
        call.resolve(o);
    }
}
