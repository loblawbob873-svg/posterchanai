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
 *   1. PICK a provider. Android's own picker (ACTION_APPWIDGET_PICK) is used rather than a
 *      hand-rolled one, because it already knows about configuration activities, profiles and
 *      restricted providers.
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

    public static final int REQ_PICK = 4501;
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

    /** Step 1: Android's own provider picker, with an id already allocated for it to fill. */
    public void pick(Activity a) {
        int id = -1;
        try {
            id = host.allocateAppWidgetId();
            Intent i = new Intent(AppWidgetManager.ACTION_APPWIDGET_PICK);
            i.putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id);
            // An empty custom list, or the picker offers our own shortcuts alongside the widgets.
            i.putParcelableArrayListExtra(AppWidgetManager.EXTRA_CUSTOM_INFO,
                    new java.util.ArrayList<android.os.Parcelable>());
            i.putParcelableArrayListExtra(AppWidgetManager.EXTRA_CUSTOM_EXTRAS,
                    new java.util.ArrayList<android.os.Parcelable>());
            a.startActivityForResult(i, REQ_PICK);
        } catch (Throwable t) {
            // The id is freed on EVERY failure path. A leaked one is a row in the system's table
            // that nothing will ever reclaim.
            if (id >= 0) release(id);
            Log.w(TAG, "home: no widget picker on this phone", t);
            // AND IT SAYS SO. A control that does nothing and says nothing is the recurring shape in
            // this feature — the dead tile, the role switch that unchecked itself — and it always
            // reads as a broken app rather than as an Android refusal.
            say(a, "no picker");
        }
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

        if (request == REQ_PICK) {
            AppWidgetProviderInfo info = infoOf(id);
            if (info == null) { release(id); return -1; }
            // BIND_APPWIDGET is signature-level, so this only succeeds where the platform has
            // already granted it. Everywhere else, ask.
            boolean bound = false;
            try { bound = manager.bindAppWidgetIdIfAllowed(id, info.provider); }
            catch (Throwable ignored) { }
            if (!bound) { askToBind(a, id, info.provider); return -1; }
            return configureOrReady(a, id);
        }
        if (request == REQ_BIND) return configureOrReady(a, id);
        if (request == REQ_CONFIGURE) return id;
        return -1;
    }

    private void askToBind(Activity a, int id, ComponentName provider) {
        try {
            Intent i = new Intent(AppWidgetManager.ACTION_APPWIDGET_BIND);
            i.putExtra(AppWidgetManager.EXTRA_APPWIDGET_ID, id);
            i.putExtra(AppWidgetManager.EXTRA_APPWIDGET_PROVIDER, provider);
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

    /** Say it on screen. BIND_APPWIDGET is signature-level, so a refusal here is normal and silent. */
    private void say(Activity a, String which) {
        if (a == null) return;
        try {
            android.widget.Toast.makeText(a,
                    "no picker".equals(which)
                        ? place.poster.app.R.string.home_no_widget_picker
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
