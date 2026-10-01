package place.poster.app;

import android.app.Activity;
import android.content.Context;
import android.os.Build;
import android.view.View;
import android.view.Window;
import android.view.WindowInsets;
import android.view.WindowInsetsController;

/**
 * HIDE THE SYSTEM BARS -- Settings -> Phone -> "Hide system bars" ("any way to hide OS dock?", on a
 * Samsung tablet showing One UI's taskbar AND PosterChan's own dock).
 *
 * Immersive mode: the status bar and the navigation bar (which on a One UI tablet carries the taskbar)
 * are hidden, and a swipe from the edge shows them for a moment. A per-DEVICE choice, so it lives in
 * this device's SharedPreferences and is applied before the page has loaded, not after.
 *
 * Android drops immersive mode on its own -- a dialog, a permission prompt, switching apps -- so it is
 * re-applied whenever the Activity resumes or its window regains focus (MainActivity). The page fills the
 * freed space by itself: MainActivity.clearSystemBars margins the WebView by the bars that are VISIBLE.
 */
public final class SystemBars {
    private static final String PREFS = "pc_system_bars";
    private static final String KEY = "hidden";

    private SystemBars() {}

    public static boolean wanted(Context c) {
        try { return c.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(KEY, false); }
        catch (Throwable t) { return false; }
    }

    public static void setWanted(Context c, boolean hidden) {
        c.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putBoolean(KEY, hidden).commit();
    }

    /** The pre-Android-11 spelling of the same thing; cleared by exactly these bits, never others. */
    @SuppressWarnings("deprecation")
    private static final int LEGACY = View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY | View.SYSTEM_UI_FLAG_FULLSCREEN
            | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION;

    /** Make the window match the stored choice. Safe to call any number of times, on the UI thread.
     *  Platform APIs only (WindowInsetsController on 11+): the local compile check has no AndroidX. */
    @SuppressWarnings("deprecation")
    public static void apply(Activity a) {
        try {
            boolean hide = wanted(a);
            Window w = a.getWindow();
            if (Build.VERSION.SDK_INT >= 30) {
                WindowInsetsController ctl = w.getInsetsController();
                if (ctl == null) return;
                if (hide) {
                    ctl.setSystemBarsBehavior(WindowInsetsController.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE);
                    ctl.hide(WindowInsets.Type.systemBars());
                } else {
                    ctl.show(WindowInsets.Type.systemBars());
                }
            } else {
                View d = w.getDecorView();
                int f = d.getSystemUiVisibility();
                d.setSystemUiVisibility(hide ? (f | LEGACY) : (f & ~LEGACY));
            }
        } catch (Throwable ignored) {
            // Never fatal: the worst case is the bars staying exactly as they were.
        }
    }
}
