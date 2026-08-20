package place.poster.app.home;

import android.app.Activity;
import android.appwidget.AppWidgetHost;
import android.appwidget.AppWidgetHostView;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProviderInfo;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;

/**
 * HOSTING OTHER APPS' WIDGETS — the clock, the weather, the calendar — on our home screen.
 *
 * A launcher without this is not a home screen, and there is no shortcut: `AppWidgetHost` is the
 * only way, and it comes with a three-step dance that is easy to get half right.
 *
 *   1. PICK a provider. OUR OWN LIST, from `getInstalledProviders()` — and that is a correction,
 *      not a preference. The first version handed this to Android by firing
 *      ACTION_APPWIDGET_PICK, on the reasoning that the platform already knows about configuration
 *      activities and restricted providers. THE PLATFORM DOES NOT ANSWER THAT INTENT. It was
 *      `com.android.settings.AppWidgetPickActivity` in the Gingerbread era, when the system dialog
 *      owned "Add to Home screen"; every launcher since has drawn its own list, and Settings
 *      stopped declaring the filter. So `startActivityForResult` threw ActivityNotFoundException,
 *      the catch freed the id and showed "This phone has no widget picker", and the answer to
 *      "no widgets can be added to posterchan launcher home screen" was that the very first step
 *      could not run — on any phone, from any of the three entry points, for the whole life of the
 *      feature. `systemPickerExists()` is kept solely so a device test can state that as a
 *      measurement rather than as a claim.
 *   2. BIND the allocated id to that provider. `BIND_APPWIDGET` is a SIGNATURE permission no
 *      third-party launcher can hold, so `bindAppWidgetIdIfAllowed` fails and the sanctioned route
 *      is ACTION_APPWIDGET_BIND, which asks the person. Skipping straight to step 3 gives a widget
 *      that draws nothing, for ever, with nothing in any log.
 *   3. CONFIGURE, if the provider asked for it. A configuration activity that is never started
 *      leaves a widget that never receives its first update — the classic "it just shows a grey
 *      box".
 *
 * AND THE ID MUST BE GIVEN BACK. An allocated id that is never bound, or a widget removed without
 * `deleteAppWidgetId`, leaks a slot in the system's own table for the life of the install. Every
 * failure path here frees it.
 *
 * `startListening`/`stopListening` bracket the host with the screen: a listening host receives every
 * update from every provider it holds, and on the one process that is resident for the life of the
 * battery that is exactly the poll this package otherwise refuses to have.
 */
public final class Widgets {

    private static final String TAG = "PosterChan";
    /** Any constant will do; it identifies OUR host to the system's widget table. */
    private static final int HOST_ID = 0x5C11;

    public static final int REQ_BIND = 4502;
    public static final int REQ_CONFIGURE = 4503;

    private final Context ctx;
    private final AppWidgetManager manager;
    private final AppWidgetHost host;
    private boolean listening = false;

    public Widgets(Context ctx) {
        this.ctx = ctx.getApplicationContext();
        this.manager = AppWidgetManager.getInstance(this.ctx);
        this.host = new AppWidgetHost(this.ctx, HOST_ID);
    }

    /** Only while the home screen is on screen — see the class comment. */
    public void start() {
        if (listening) return;
        try { host.startListening(); listening = true; }
        catch (Throwable t) { Log.w(TAG, "home: widget host would not start", t); }
    }

    public void stop() {
        if (!listening) return;
        try { host.stopListening(); } catch (Throwable ignored) { }
        listening = false;
    }

    /**
     * ONE WIDGET SOMEBODY MAY ADD. `info` is what the rest of the flow needs; the two labels are
     * what makes the list readable — a phone has dozens of providers and half of them are called
     * "Clock".
     */
    public static final class Choice {
        public final AppWidgetProviderInfo info;
        public final String label;
        public final String appLabel;
        public final int spanX, spanY;
        Choice(AppWidgetProviderInfo info, String label, String appLabel, int spanX, int spanY) {
            this.info = info; this.label = label; this.appLabel = appLabel;
            this.spanX = spanX; this.spanY = spanY;
        }
        public ComponentName provider() { return info.provider; }
    }

    /**
     * STEP 1: EVERY WIDGET ON THIS PHONE, asked of the widget manager itself.
     *
     * Sorted by the owning app and then by the widget's own label, because that is the order the
     * person is scanning in — they are looking for "the clock in Google Clock", not for a provider
     * class name.
     *
     * `cellDp` is only used to state a size in the list ("4 x 1"); a provider that reports nothing
     * useful still appears, at 1 x 1, rather than being filtered out — a widget missing from the
     * list is indistinguishable from the bug this method was written to fix.
     */
    public java.util.List<Choice> providers(int cellWdp, int cellHdp) {
        java.util.List<Choice> out = new java.util.ArrayList<Choice>();
        java.util.List<AppWidgetProviderInfo> all;
        try { all = manager.getInstalledProviders(); }
        catch (Throwable t) { Log.w(TAG, "home: the widget manager would not list providers", t); return out; }
        if (all == null) return out;
        android.content.pm.PackageManager pm = ctx.getPackageManager();
        for (AppWidgetProviderInfo i : all) {
            if (i == null || i.provider == null) continue;
            String label = null;
            try { label = i.loadLabel(pm); } catch (Throwable ignored) { }
            if (label == null || label.trim().isEmpty()) label = i.provider.getShortClassName();
            String app = i.provider.getPackageName();
            try {
                CharSequence c = pm.getApplicationLabel(pm.getApplicationInfo(app, 0));
                if (c != null && c.length() > 0) app = c.toString();
            } catch (Throwable ignored) { }
            out.add(new Choice(i, label.trim(), app,
                    spanFor(i.minWidth, cellWdp), spanFor(i.minHeight, cellHdp)));
        }
        java.util.Collections.sort(out, new java.util.Comparator<Choice>() {
            @Override public int compare(Choice a, Choice b) {
                int n = a.appLabel.compareToIgnoreCase(b.appLabel);
                return n != 0 ? n : a.label.compareToIgnoreCase(b.label);
            }
        });
        return out;
    }

    /** The little picture the picker draws. Preview if the provider has one, its icon if not. */
    public android.graphics.drawable.Drawable preview(Choice c) {
        if (c == null) return null;
        int density = 0;
        try { density = ctx.getResources().getDisplayMetrics().densityDpi; } catch (Throwable ignored) { }
        try {
            android.graphics.drawable.Drawable d = c.info.loadPreviewImage(ctx, density);
            if (d != null) return d;
        } catch (Throwable ignored) { }
        try { return c.info.loadIcon(ctx, density); } catch (Throwable ignored) { }
        return null;
    }

    /**
     * STEPS 2 AND 3, started. Returns the widget id when it is ready to place RIGHT NOW (already
     * allowed to bind and no configuration activity), or -1 — which means either "an activity is
     * asking the person something, wait for onActivityResult" or "it was refused, and has said so".
     *
     * The id is allocated here and freed on every path that does not hand it back.
     */
    public int add(Activity a, Choice c) {
        if (c == null) return -1;
        int id = -1;
        try {
            id = host.allocateAppWidgetId();
            boolean bound = false;
            try {
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                    bound = manager.bindAppWidgetIdIfAllowed(id, profileOf(c.info), c.info.provider, null);
                } else {
                    bound = manager.bindAppWidgetIdIfAllowed(id, c.info.provider);
                }
            } catch (Throwable ignored) { }
            if (!bound) { askToBind(a, id, c.info); return -1; }
            return configureOrReady(a, id);
        } catch (Throwable t) {
            if (id >= 0) release(id);
            Log.w(TAG, "home: could not add a widget", t);
            say(a, "refused");
            return -1;
        }
    }

    private static android.os.UserHandle profileOf(AppWidgetProviderInfo i) {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP && i.getProfile() != null) {
                return i.getProfile();
            }
        } catch (Throwable ignored) { }
        return android.os.Process.myUserHandle();
    }

    /**
     * DOES THIS IMAGE ANSWER ACTION_APPWIDGET_PICK? Nothing in the flow depends on it any more; it
     * exists so a device test can print the answer. Believing it did is what made the whole feature
     * dead on arrival, and the belief survived three rounds of fixes because a missing activity
     * throws in exactly the same place a cancelled dialog returns.
     */
    public boolean systemPickerExists() {
        try {
            Intent i = new Intent(AppWidgetManager.ACTION_APPWIDGET_PICK);
            return ctx.getPackageManager().resolveActivity(i, 0) != null;
        } catch (Throwable t) { return false; }
    }

    /** Whether Android will even offer to ask the person about binding. */
    public boolean bindDialogExists() {
        try {
            Intent i = new Intent(AppWidgetManager.ACTION_APPWIDGET_BIND);
            return ctx.getPackageManager().resolveActivity(i, 0) != null;
        } catch (Throwable t) { return false; }
    }

    /**
     * Step 2 and 3, from onActivityResult. Returns the widget id once it is genuinely ready to be
     * placed, or -1 while it is not (still asking, or refused — either way the caller does nothing).
     */
    public int onResult(Activity a, int request, int result, Intent data) {
        int id = data == null ? -1
               : data.getIntExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, -1);
        if (result != Activity.RESULT_OK) {
            // Cancelled at any step: give the id back. This is the path people take most often —
            // opening the picker and changing their mind.
            if (id >= 0) release(id);
            return -1;
        }
        if (id < 0) return -1;

        if (request == REQ_BIND) return configureOrReady(a, id);
        if (request == REQ_CONFIGURE) return id;
        return -1;
    }

    /**
     * ASK. `BIND_APPWIDGET` is signature-level, so on a third-party launcher this is the ONLY route
     * and it is normal, not exceptional — the person sees "Allow PosterChan to create widgets?".
     *
     * EXTRA_APPWIDGET_PROVIDER_PROFILE is not decoration: without it a widget belonging to a work
     * profile binds against the personal user and the dialog refuses, which looks from the outside
     * like the person said no.
     */
    private void askToBind(Activity a, int id, AppWidgetProviderInfo info) {
        try {
            Intent i = new Intent(AppWidgetManager.ACTION_APPWIDGET_BIND);
            i.putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id);
            i.putExtra(AppWidgetManager.EXTRA_APPWIDGET_PROVIDER, info.provider);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
                i.putExtra(AppWidgetManager.EXTRA_APPWIDGET_PROVIDER_PROFILE, profileOf(info));
            }
            a.startActivityForResult(i, REQ_BIND);
        } catch (Throwable t) {
            release(id);
            Log.w(TAG, "home: could not ask to bind a widget", t);
            say(a, "refused");
        }
    }

    /** Start the provider's configuration activity if it has one; otherwise it is ready now. */
    private int configureOrReady(Activity a, int id) {
        AppWidgetProviderInfo info = infoOf(id);
        if (info == null) { release(id); return -1; }
        if (info.configure == null) return id;
        try {
            Intent i = new Intent(AppWidgetManager.ACTION_APPWIDGET_CONFIGURE);
            i.setComponent(info.configure);
            i.putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id);
            a.startActivityForResult(i, REQ_CONFIGURE);
        } catch (Throwable t) {
            // A provider whose configuration activity refuses to start is still a usable widget —
            // it simply arrives with its defaults. Dropping it here would be worse.
            Log.w(TAG, "home: widget configuration would not open", t);
            return id;
        }
        return -1;
    }

    /** Say it on screen. Every Android refusal in this flow is silent; none of ours is. */
    void say(Activity a, String which) {
        if (a == null) return;
        try {
            android.widget.Toast.makeText(a,
                    "no picker".equals(which)
                        ? place.poster.app.R.string.home_no_widgets
                        : place.poster.app.R.string.home_widget_refused,
                    android.widget.Toast.LENGTH_LONG).show();
        } catch (Throwable ignored) { }
    }

    public AppWidgetProviderInfo infoOf(int id) {
        try { return manager.getAppWidgetInfo(id); } catch (Throwable t) { return null; }
    }

    /** The view to put on the desktop, or null if the widget has gone (its app was uninstalled). */
    public AppWidgetHostView view(Context themed, int id) {
        AppWidgetProviderInfo info = infoOf(id);
        if (info == null) return null;
        try { return host.createView(themed, id, info); }
        catch (Throwable t) { Log.w(TAG, "home: widget would not draw", t); return null; }
    }

    /**
     * THE IDS THIS HOST CURRENTLY HOLDS. Only a device test reads it, and only to prove that an
     * abandoned add left nothing behind — a leaked id is a row in the system's own widget table that
     * nothing will ever reclaim, and the picker is where people change their mind most often.
     */
    int[] hostIds() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) return host.getAppWidgetIds();
        } catch (Throwable ignored) { }
        return new int[0];
    }

    /** Give the id back to the system. Called on every removal and every abandoned add. */
    public void release(int id) {
        try { host.deleteAppWidgetId(id); } catch (Throwable ignored) { }
    }

    /**
     * TELL THE PROVIDER HOW BIG IT NOW IS. A resize that only changes our layout leaves the widget
     * drawing its old size's content — a 4x1 clock stretched across a 4x2 hole. `updateAppWidgetOptions`
     * is what makes a responsive widget actually respond.
     */
    public void resized(int id, int minWdp, int minHdp, int maxWdp, int maxHdp) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.JELLY_BEAN) return;
        try {
            Bundle o = new Bundle();
            o.putInt(AppWidgetManager.OPTION_APPWIDGET_MIN_WIDTH, minWdp);
            o.putInt(AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT, minHdp);
            o.putInt(AppWidgetManager.OPTION_APPWIDGET_MAX_WIDTH, maxWdp);
            o.putInt(AppWidgetManager.OPTION_APPWIDGET_MAX_HEIGHT, maxHdp);
            manager.updateAppWidgetOptions(id, o);
        } catch (Throwable ignored) { }
    }

    /** How many cells this provider needs at minimum, and whether it may be resized at all. */
    public static int spanFor(int minDp, int cellDp) {
        if (cellDp <= 0) return 1;
        return Math.max(1, (int) Math.ceil(minDp / (double) cellDp));
    }

    public boolean resizableWide(int id) {
        AppWidgetProviderInfo i = infoOf(id);
        return i != null && (i.resizeMode & AppWidgetProviderInfo.RESIZE_HORIZONTAL) != 0;
    }

    public boolean resizableTall(int id) {
        AppWidgetProviderInfo i = infoOf(id);
        return i != null && (i.resizeMode & AppWidgetProviderInfo.RESIZE_VERTICAL) != 0;
    }
}
