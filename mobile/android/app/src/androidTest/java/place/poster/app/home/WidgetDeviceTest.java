package place.poster.app.home;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.app.Instrumentation;
import android.appwidget.AppWidgetHostView;
import android.appwidget.AppWidgetProviderInfo;
import android.content.Context;
import android.os.Build;
import android.util.Log;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;

/**
 * ADDING A WIDGET, DRIVEN END TO END ON A REAL ANDROID.
 *
 * "no widgets can be added to posterchan launcher home screen" survived a round of fixes — a real
 * AppWidgetHost, Android's own picker, drag-resize, ACTION_APPWIDGET_BIND, and then three menu
 * entries so the flow could be found. It survived them because none of them was the problem. Step 1
 * fired ACTION_APPWIDGET_PICK, nothing on a modern Android answers that intent, and the catch that
 * was meant to explain an exotic failure was on the ONLY path.
 *
 * Nothing about that is visible from a source file: `startActivityForResult` throwing
 * ActivityNotFoundException and a person cancelling the dialog land in code two lines apart. It is
 * visible from a device in one call, which is what this file is.
 *
 * IT COMPLETES THE FLOW RATHER THAN INSPECTING IT. `appwidget grantbind` is the shell equivalent of
 * somebody tapping "Allow" on the bind dialog — the one step in the sequence that needs a person —
 * so allocate, bind, configure-or-ready, draw and place all run for real, and the widget id is given
 * back at the end and checked to have gone.
 */
@RunWith(AndroidJUnit4.class)
public class WidgetDeviceTest {

    private static final String TAG = "PosterChan";

    private Context ctx;
    private Widgets widgets;
    private boolean wasEnabled;
    private final List<Integer> allocated = new ArrayList<Integer>();

    @Before
    public void setUp() {
        ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        wasEnabled = HomeRoles.launcherComponentEnabled(ctx);
        widgets = new Widgets(ctx);
        widgets.start();
    }

    @After
    public void tearDown() {
        for (Integer id : allocated) widgets.release(id);
        allocated.clear();
        widgets.stop();
        // Both are put back: leaving the launcher component on would mean the next test in this boot
        // inherits a phone that offers a home-screen chooser, and leaving the bind permission
        // granted would let a later change assume a permission no real install has.
        HomeRoles.enableLauncherComponent(ctx, wasEnabled);
        shell("appwidget revokebind --package " + ctx.getPackageName() + " --user 0");
    }

    @Test
    public void theSystemWidgetPickerIsNotSomethingToBuildOn() {
        // THE DIAGNOSIS, STATED AS A MEASUREMENT. ACTION_APPWIDGET_PICK was answered by
        // `com.android.settings.AppWidgetPickActivity` in the era when a system dialog owned "Add to
        // Home screen"; every launcher since has drawn its own list. If this ever comes back true on
        // some image, that is worth knowing — but the flow must not depend on it either way, which
        // is what the next test proves.
        boolean picker = widgets.systemPickerExists();
        boolean bind = widgets.bindDialogExists();
        Log.i(TAG, "widget probe: API " + Build.VERSION.SDK_INT
                + " systemPicker=" + picker + " bindDialog=" + bind);
        assertFalse("ACTION_APPWIDGET_PICK resolves on API " + Build.VERSION.SDK_INT
                + " after all — the flow still must not rely on it, but the diagnosis that step 1"
                + " could never run needs re-checking against the reporter's phone", picker);
        // The bind dialog is the one system activity the flow DOES need, on every phone where
        // BIND_APPWIDGET is not already granted — which is every phone.
        assertTrue("no ACTION_APPWIDGET_BIND activity: a third-party launcher cannot bind a widget"
                + " at all on this image", bind);
    }

    @Test
    public void thisPhoneOffersWidgetsAndWeCanSeeThemAll() {
        List<Widgets.Choice> rows = widgets.providers(90, 90);
        StringBuilder b = new StringBuilder();
        for (Widgets.Choice c : rows) {
            b.append("\n    ").append(c.appLabel).append(" / ").append(c.label)
             .append("  ").append(c.spanX).append("x").append(c.spanY)
             .append("  ").append(c.provider().flattenToShortString());
        }
        Log.i(TAG, "widget probe: " + rows.size() + " providers" + b);
        assertTrue("the widget manager listed no providers at all on this image — with the system"
                + " picker gone this list IS the picker, so an empty one is an empty screen",
                rows.size() > 0);
        // Sorted by app then by widget, because that is the order somebody scans in.
        for (int i = 1; i < rows.size(); i++) {
            String prev = rows.get(i - 1).appLabel, cur = rows.get(i).appLabel;
            assertTrue("the list is not in app order: " + prev + " before " + cur,
                    prev.compareToIgnoreCase(cur) <= 0);
        }
    }

    @Test
    public void theEntireAddAWidgetFlowRunsOnThisDevice() {
        // Step 1: our own list.
        List<Widgets.Choice> rows = widgets.providers(90, 90);
        assertTrue("nothing to add", rows.size() > 0);
        Widgets.Choice pick = null;
        for (Widgets.Choice c : rows) if (c.info.configure == null) { pick = c; break; }
        assertNotNull("every provider on this image demands a configuration activity, which needs a"
                + " person — the flow cannot be completed headlessly here", pick);
        Log.i(TAG, "widget probe: adding " + pick.provider().flattenToShortString());

        // Step 2: the bind. `bindAppWidgetIdIfAllowed` is FALSE for a third-party launcher until the
        // person says yes — that refusal is the normal path, not the error path, and asserting it
        // here is what stops a future change from quietly assuming the permission.
        Widgets probe = new Widgets(ctx);
        int id = probe.add(null, pick);
        assertEquals("bindAppWidgetIdIfAllowed succeeded without the permission — then the bind"
                + " dialog is dead code and nothing will notice when it breaks", -1, id);

        // …and this is that person tapping Allow.
        String granted = shell("appwidget grantbind --package " + ctx.getPackageName() + " --user 0");
        Log.i(TAG, "widget probe: grantbind -> " + granted.trim());

        final Widgets.Choice chosen = pick;
        final int[] out = new int[]{ -1 };
        HomeRoles.enableLauncherComponent(ctx, true);
        ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
        try {
            s.onActivity(a -> out[0] = widgets.add(a, chosen));
        } finally {
            s.close();
        }
        int widgetId = out[0];
        assertTrue("the widget was not bound even with the permission granted — id " + widgetId,
                widgetId >= 0);
        allocated.add(widgetId);

        // Step 3: it is a real, bound widget belonging to the provider that was picked.
        AppWidgetProviderInfo info = widgets.infoOf(widgetId);
        assertNotNull("the id bound to nothing", info);
        assertEquals(chosen.provider(), info.provider);

        // Step 4: IT DRAWS. `createView` returning null is the shape of the "grey box forever" bug,
        // and it is the only step that can fail after everything else reports success.
        final AppWidgetHostView[] view = new AppWidgetHostView[1];
        InstrumentationRegistry.getInstrumentation().runOnMainSync(
                () -> view[0] = widgets.view(ctx, widgetId));
        assertNotNull("the host would not build a view for a bound widget", view[0]);

        // Step 5: it fits on the grid it is being put on.
        List<Desk.Item> items = new ArrayList<Desk.Item>();
        int sx = Math.min(4, Widgets.spanFor(info.minWidth, 90));
        int sy = Math.min(5, Widgets.spanFor(info.minHeight, 90));
        Desk.Item it = new Desk.Item(Desk.widgetKey(widgetId), 0, 0, sx, sy);
        assertTrue("a freshly added widget did not fit on an empty desktop (" + sx + "x" + sy + ")",
                Desk.add(items, it, 4, 5));
        assertEquals(widgetId, items.get(0).widgetId());

        Log.i(TAG, "widget probe: added id=" + widgetId + " span=" + sx + "x" + sy + " OK");
    }

    @Test
    public void anAbandonedWidgetGivesItsIdBack() {
        // A leaked id is a row in the system's own widget table that nothing will ever reclaim, and
        // the picker is the place people change their mind most often.
        if (Build.VERSION.SDK_INT < 26) return;      // getAppWidgetIds() is API 26
        List<Widgets.Choice> rows = widgets.providers(90, 90);
        assertTrue(rows.size() > 0);
        shell("appwidget grantbind --package " + ctx.getPackageName() + " --user 0");
        Widgets w = new Widgets(ctx);
        int before = countIds(w);
        int id = w.add(null, rows.get(0));
        if (id >= 0) w.release(id);
        assertEquals("an added-then-removed widget left its id behind", before, countIds(w));
    }

    private static int countIds(Widgets w) {
        int[] ids = w.hostIds();
        return ids == null ? 0 : ids.length;
    }

    private static String shell(String cmd) {
        Instrumentation in = InstrumentationRegistry.getInstrumentation();
        try (InputStream is = new android.os.ParcelFileDescriptor.AutoCloseInputStream(
                in.getUiAutomation().executeShellCommand(cmd))) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = is.read(buf)) > 0) out.write(buf, 0, n);
            return out.toString("UTF-8");
        } catch (Throwable t) {
            return "shell failed: " + t;
        }
    }
}
