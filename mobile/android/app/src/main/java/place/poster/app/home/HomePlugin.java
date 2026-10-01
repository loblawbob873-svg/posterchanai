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

    /**
     * THE SIGNAL THAT CANNOT BE COALESCED AWAY.
     *
     * A tile pressed while the app is already running has no page load to hang the read off, so the
     * client listened for `visibilitychange` — which on Android arrives late or not at all, and a
     * landing that arrives late is a person looking at the wrong screen. This is pushed from
     * `MainActivity.onNewIntent`, i.e. the moment the press actually lands, from the Activity, which
     * Android never freezes.
     *
     * STATIC for the same reason SmsPlugin's `live` is: the caller is not the plugin and frequently
     * runs when there is no plugin instance at all. It carries NO payload — the client still calls
     * `consumeLaunchView`, so there is exactly one consumer of the parked request and no second path
     * that could disagree with it.
     */
    private static volatile HomePlugin live;

    @Override
    public void load() { live = this; }

    @Override
    protected void handleOnDestroy() { if (live == this) live = null; }

    /** Tell the page there may be a launch request to read. Safe to call when nothing is listening. */
    public static void announceLaunchView(String requested) {
        HomePlugin p = live;
        if (p == null) return;
        try {
            JSObject o = new JSObject();
            o.put("view", requested == null ? "" : requested.trim());
            p.notifyListeners("launchView", o);
        } catch (Throwable ignored) { }
    }

    public static void announceLaunchView() { announceLaunchView(""); }

    /** Settings -> Phone -> "Hide system bars": store the choice for this device and apply it now. */
    @PluginMethod
    public void setSystemBarsHidden(PluginCall call) {
        final boolean hidden = Boolean.TRUE.equals(call.getBoolean("hidden", false));
        try {
            place.poster.app.SystemBars.setWanted(getContext(), hidden);
        } catch (Throwable t) {
            call.reject("could not save the setting");
            return;
        }
        final android.app.Activity a = getActivity();
        if (a != null) a.runOnUiThread(() -> place.poster.app.SystemBars.apply(a));
        JSObject o = new JSObject();
        o.put("hidden", hidden);
        call.resolve(o);
    }

    @PluginMethod
    public void systemBars(PluginCall call) {
        JSObject o = new JSObject();
        o.put("hidden", place.poster.app.SystemBars.wanted(getContext()));
        call.resolve(o);
    }

    /**
     * Settings -> Phone -> "Screen report": what THIS phone says about its bars and where the page sits.
     *
     * "Many things are cut off at the top" was fixed twice by measurement and both times the measurement
     * was right on the emulator and wrong on the phone (a Galaxy S25 on One UI 8; a 1080x2412 phone in a
     * bug report): the page sits partly under the status bar, by an amount no fixture produces. This
     * returns the raw numbers -- every inset the window reports, in both spellings, the window and the
     * WebView ON SCREEN, the margin applied, and the platform's own status_bar_height -- so the next fix is
     * made from that phone's facts. Platform APIs only: the local compile check has no AndroidX.
     */
    @PluginMethod
    public void screenReport(PluginCall call) {
        final android.app.Activity a = getActivity();
        if (a == null) { call.reject("no activity"); return; }
        a.runOnUiThread(() -> {
            JSObject o = new JSObject();
            try {
                android.util.DisplayMetrics dm = a.getResources().getDisplayMetrics();
                o.put("model", android.os.Build.MANUFACTURER + " " + android.os.Build.MODEL);
                o.put("sdk", android.os.Build.VERSION.SDK_INT);
                o.put("density", dm.density);
                o.put("screen", dm.widthPixels + "x" + dm.heightPixels);
                int sbRes = a.getResources().getIdentifier("status_bar_height", "dimen", "android");
                o.put("statusBarHeightRes", sbRes > 0 ? a.getResources().getDimensionPixelSize(sbRes) : -1);
                android.view.View decor = a.getWindow().getDecorView();
                int[] d = new int[2];
                decor.getLocationOnScreen(d);
                o.put("windowTopOnScreen", d[1]);
                o.put("windowHeight", decor.getHeight());
                android.view.View wv = getBridge().getWebView();
                int[] w = new int[2];
                wv.getLocationOnScreen(w);
                int[] wi = new int[2];
                wv.getLocationInWindow(wi);
                o.put("pageTopOnScreen", w[1]);
                o.put("pageTopInWindow", wi[1]);
                o.put("pageBottomOnScreen", w[1] + wv.getHeight());
                android.view.ViewGroup.LayoutParams lp = wv.getLayoutParams();
                if (lp instanceof android.view.ViewGroup.MarginLayoutParams) {
                    android.view.ViewGroup.MarginLayoutParams m = (android.view.ViewGroup.MarginLayoutParams) lp;
                    o.put("pageMargins", m.leftMargin + "," + m.topMargin + "," + m.rightMargin + "," + m.bottomMargin);
                }
                o.put("cutoutMode", a.getWindow().getAttributes().layoutInDisplayCutoutMode);
                android.view.WindowInsets in = decor.getRootWindowInsets();
                if (in != null) {
                    o.put("legacyTop", in.getSystemWindowInsetTop());
                    o.put("legacyBottom", in.getSystemWindowInsetBottom());
                    o.put("stableTop", in.getStableInsetTop());
                    if (android.os.Build.VERSION.SDK_INT >= 30) {
                        o.put("statusTop", in.getInsets(android.view.WindowInsets.Type.statusBars()).top);
                        o.put("statusTopIgnoringVisibility",
                                in.getInsetsIgnoringVisibility(android.view.WindowInsets.Type.statusBars()).top);
                        o.put("statusVisible", in.isVisible(android.view.WindowInsets.Type.statusBars()));
                        o.put("navBottom", in.getInsets(android.view.WindowInsets.Type.navigationBars()).bottom);
                        o.put("cutoutTop", in.getInsets(android.view.WindowInsets.Type.displayCutout()).top);
                        o.put("imeBottom", in.getInsets(android.view.WindowInsets.Type.ime()).bottom);
                    }
                } else {
                    o.put("insets", "none");
                }
                // The one number that matters: how much of the page is under the status bar right now.
                Object st = o.has("statusTopIgnoringVisibility") ? o.get("statusTopIgnoringVisibility")
                          : o.has("stableTop") ? o.get("stableTop") : 0;
                o.put("overlapTop", Math.max(0, d[1] + ((Number) st).intValue() - w[1]));
            } catch (Throwable t) {
                o.put("error", String.valueOf(t));
            }
            call.resolve(o);
        });
    }

    /** Phone or tablet, from the device's own configuration -- see FormFactor. */
    @PluginMethod
    public void formFactor(PluginCall call) {
        boolean voice = true;
        try {
            android.telephony.TelephonyManager tm = (android.telephony.TelephonyManager)
                    getContext().getSystemService(android.content.Context.TELEPHONY_SERVICE);
            voice = tm != null && tm.isVoiceCapable();
        } catch (Throwable ignored) { }
        JSObject o = new JSObject();
        o.put("form", FormFactor.classify(voice));
        o.put("voiceCapable", voice);
        try { o.put("swDp", getContext().getResources().getConfiguration().smallestScreenWidthDp); }
        catch (Throwable ignored) { }
        call.resolve(o);
    }

    @PluginMethod
    public void status(PluginCall call) {
        JSObject o = new JSObject();
        o.put("sdk", android.os.Build.VERSION.SDK_INT);
        o.put("launcherEnabled", HomeRoles.launcherComponentEnabled(getContext()));
        // What the settings screen needs to tell the truth about the icons, measured rather than
        // inferred from the opt-in flag — a package replacement can re-apply the manifest default.
        o.put("drawerIcons", HomeRoles.drawerIconEnabled(getContext(), HomeRoles.DRAWER_ICONS[0]));
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
        /* WHICH PART IS MISSING, not just that one is. "it is impossible to check Messages in User
           Settings -> Phone, nothing happend" — and the switch's answer for the not-capable case was
           "update the app and try again", which is advice nobody can act on and which is wrong
           whenever the build is already current. Android demands four components before it will
           offer the SMS role; naming the absent one turns a dead switch into something reportable. */
        JSObject parts = new JSObject();
        parts.put("smsDeliver", HomeRoles.hasSmsDeliver(getContext()));
        parts.put("mmsDeliver", HomeRoles.hasMmsDeliver(getContext()));
        parts.put("sendTo", HomeRoles.hasSendTo(getContext()));
        parts.put("respondViaMessage", HomeRoles.hasRespondService(getContext()));
        o.put("smsParts", parts);
        o.put("dialerCapable", HomeRoles.canBeDialer(getContext()));
        call.resolve(o);
    }

    @PluginMethod
    public void enableLauncher(PluginCall call) {
        try {
            Intent i = HomeRoles.requestHome(getContext());
            new LauncherPrefs(getContext()).setOptedIn(true);
            /* THE DRAWER ICONS FOLLOW THE OPT-IN, and they ship off. Texts, Phone, Media Center and
             * Email are LAUNCHER aliases, so before this they appeared in everybody's app drawer the
             * moment the APK was installed — on phones where the shell was never enabled and those
             * screens do nothing. Enabled here, with the rest of the shell, is what makes the icons
             * mean something. */
            HomeRoles.setAllDrawerIcons(getContext(), true);
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
        if (ok) {
            new LauncherPrefs(getContext()).setOptedIn(false);
            // …and they go away again with it. An icon left behind after the feature is switched
            // off is the same complaint one step later.
            HomeRoles.setAllDrawerIcons(getContext(), false);
        }
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
