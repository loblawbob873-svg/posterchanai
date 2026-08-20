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
    public void whatTheSystemWidgetPickerActuallyIsOnThisImage() {
        // A THEORY THIS DEVICE REFUTED, KEPT AS A MEASUREMENT.
        //
        // The first version of this test asserted that ACTION_APPWIDGET_PICK resolves to nothing on
        // a modern Android — the tidy explanation for why "Add a widget" could never work. THE
        // EMULATOR SAID `systemPicker=true` ON API 34, so that explanation is wrong, at least here,
        // and it is not repeated anywhere as fact.
        //
        // Resolving is not the same as being usable, which is the distinction the original guess
        // skipped: a third-party app calling `startActivityForResult` on an activity that is not
        // exported, or that is guarded by a permission it does not hold, gets an exception in the
        // same catch a cancelled dialog returns quietly through. So this prints what the activity
        // IS — package, exported, permission — and leaves the conclusion to whoever reads it.
        //
        // What does NOT depend on the answer: our own list. Every launcher builds one, it works on
        // an image with no picker and on an image with a picker we may not start, and it is the only
        // version of this flow that can show a preview or say "no app on this phone offers a widget".
        boolean picker = widgets.systemPickerExists();
        boolean bind = widgets.bindDialogExists();
        StringBuilder detail = new StringBuilder();
        try {
            android.content.pm.ResolveInfo ri = ctx.getPackageManager().resolveActivity(
                    new android.content.Intent(android.appwidget.AppWidgetManager.ACTION_APPWIDGET_PICK), 0);
            if (ri == null) detail.append("resolves to nothing");
            else detail.append(ri.activityInfo.packageName).append('/').append(ri.activityInfo.name)
                       .append(" exported=").append(ri.activityInfo.exported)
                       .append(" permission=").append(ri.activityInfo.permission);
        } catch (Throwable t) { detail.append("query threw ").append(t); }
        Log.i(TAG, "widget probe: API " + Build.VERSION.SDK_INT
                + " systemPicker=" + picker + " bindDialog=" + bind + " -> " + detail);

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
        // Sorted by app then by widget, because that is the order somebody scans in — EXCEPT for
        // our own block, which is deliberately first (Widgets.providers). This assertion predated
        // that change and duly failed with "PosterChan before Calendar", which is the sort working.
        int i = 0;
        while (i < rows.size()
               && ctx.getPackageName().equals(rows.get(i).provider().getPackageName())) i++;
        for (int j = i + 1; j < rows.size(); j++) {
            String prev = rows.get(j - 1).appLabel, cur = rows.get(j).appLabel;
            assertTrue("the list is not in app order after our own block: " + prev + " before " + cur,
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
    public void ourOwnWidgetsAreNamedAndNotJustThreeCopiesOfTheAppName() {
        // "You have no idea which widget you are adding." A widget's label is its RECEIVER's label,
        // and without one `loadLabel` falls back to the APPLICATION label — so both of ours listed
        // as "PosterChan / PosterChan" on this very emulator while every other app's read
        // "Clock / Analog". Measured from the provider dump, not guessed.
        java.util.Map<String, String> ours = new java.util.HashMap<String, String>();
        for (Widgets.Choice c : widgets.providers(90, 90)) {
            if (ctx.getPackageName().equals(c.provider().getPackageName())) {
                ours.put(c.provider().getShortClassName(), c.label);
            }
        }
        assertTrue("PosterChan offers no widgets of its own: " + ours, ours.size() >= 3);
        assertEquals("Calendar", ours.get(".calendar.CalendarWidget"));
        assertEquals("Music", ours.get(".music.MusicWidget"));
        assertEquals("Weather", ours.get(".weather.WeatherWidget"));
        assertEquals("two of our widgets share a name: " + ours,
                ours.size(), new java.util.HashSet<String>(ours.values()).size());
    }

    @Test
    public void ourThreeAreTogetherAtTheTopUnderOneHeading() {
        // "our own three widgets are sitting in that list and the user could not identify them."
        // Grouped by app so the list is scannable, and OURS FIRST — this is PosterChan's launcher and
        // PosterChan's widgets were the ones nobody could find, scattered alphabetically between
        // Photos and System UI. Everything after them stays alphabetical, so nothing is hidden.
        java.util.List<Widgets.Choice> rows = widgets.providers(90, 90);
        assertTrue("nothing to group", rows.size() >= 3);
        int mine = 0;
        for (Widgets.Choice c : rows) {
            if (ctx.getPackageName().equals(c.provider().getPackageName())) mine++; else break;
        }
        assertEquals("PosterChan's own widgets are not the first rows in the picker", 3, mine);
        // One contiguous run: nothing of ours further down.
        int total = 0;
        for (Widgets.Choice c : rows) {
            if (ctx.getPackageName().equals(c.provider().getPackageName())) total++;
        }
        assertEquals("our widgets are split across the list", 3, total);
        // And every other app's rows are contiguous too, or a header would appear twice.
        java.util.Set<String> seen = new java.util.HashSet<String>();
        String cur = null;
        for (Widgets.Choice c : rows) {
            if (!c.appLabel.equals(cur)) {
                assertTrue("'" + c.appLabel + "' appears in two places, so its heading would too",
                        seen.add(c.appLabel));
                cur = c.appLabel;
            }
        }
    }

    @Test
    public void ourOwnWidgetsDrawARealPreviewAndNotJustTheAppIcon() {
        // THE COORDINATOR'S FIRST-THING-TO-CHECK: do ours look worse than a third-party row?
        // None of our three sets `android:previewImage`; all three set `previewLayout`, which
        // `loadPreviewImage` does not render. Without the inflation step they would every one fall
        // through to the app icon — three identical PosterChan marks, which is the complaint.
        // A BitmapDrawable here means the layout was actually inflated and drawn.
        for (Widgets.Choice c : widgets.providers(90, 90)) {
            if (!ctx.getPackageName().equals(c.provider().getPackageName())) continue;
            android.graphics.drawable.Drawable d = widgets.preview(c);
            assertNotNull(c.label + " has no picture at all", d);
            assertTrue(c.label + " fell through to an icon — its previewLayout was not rendered,"
                    + " so all three of ours draw the same PosterChan mark",
                    d instanceof android.graphics.drawable.BitmapDrawable);
        }
    }

    @Test
    public void everyWidgetInTheListHasAPictureToChooseBy() {
        // A row with no art is a row you cannot choose by, which is the complaint. The chain is
        // previewImage -> previewLayout (API 31+, which is what modern providers ship INSTEAD and
        // which loadPreviewImage does not render) -> the provider's icon -> the app's icon.
        java.util.List<Widgets.Choice> rows = widgets.providers(90, 90);
        assertTrue(rows.size() > 0);
        java.util.List<String> blank = new java.util.ArrayList<String>();
        for (Widgets.Choice c : rows) {
            if (widgets.preview(c) == null) blank.add(c.appLabel + "/" + c.label);
        }
        assertTrue("no picture for: " + blank, blank.isEmpty());
    }

    @Test
    public void aPlacedWidgetCanBeLongPressedWhichIsHowItIsREMOVED() throws Exception {
        // "no way to remove widgets". The menu with Remove in it hangs off a long press on the
        // desktop, and an AppWidgetHostView's RemoteViews children are CLICKABLE — they consume the
        // DOWN, so DeskView was never told a finger had gone down on a widget and the long press was
        // never armed. Moving and resizing died with it.
        //
        // `setItems` already did `v.setClickable(false)` on the host view (66c7f2ec) and that could
        // never have worked: it clears the flag on the AppWidgetHostView ITSELF, while the views
        // that consume the touch are the RemoteViews DESCENDANTS inside it. The probe below prints
        // `clickableContent=true` on a widget whose host view is not clickable, which is the whole
        // bug in one line.
        //
        // DRIVEN AGAINST A DESKVIEW OF OUR OWN, not the live launcher's. Two earlier runs failed
        // here with the widget missing from cell (0,0) and the item put down — because
        // HomeActivity redraws its desktop from stored preferences after layout, and `setItems`
        // clears `editing`. That is the activity doing its job, arriving mid-gesture; it is not the
        // thing under test, and it made a passing product look like a broken one.
        shell("appwidget grantbind --package " + ctx.getPackageName() + " --user 0");
        java.util.List<Widgets.Choice> rows = widgets.providers(90, 90);
        Widgets.Choice pick = null;
        for (Widgets.Choice c : rows) if (c.info.configure == null) { pick = c; break; }
        assertNotNull("nothing addable without a person", pick);

        final Widgets.Choice chosen = pick;
        final int[] made = new int[]{ -1 };
        HomeRoles.enableLauncherComponent(ctx, true);
        ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
        try {
            s.onActivity(a -> made[0] = widgets.add(a, chosen));
            assertTrue("could not place a widget to press", made[0] >= 0);
            allocated.add(made[0]);

            final DeskView[] deskOut = new DeskView[1];
            final Desk.Item[] pressed = new Desk.Item[1];
            final boolean[] clickable = new boolean[]{ false };

            s.onActivity(a -> {
                final int id = made[0];
                DeskView d = new DeskView(a);
                d.bind(new DeskView.Host() {
                    @Override public android.view.View viewFor(Desk.Item item) { return widgets.view(a, id); }
                    @Override public void onOpen(Desk.Item item) { }
                    // THE ACTUAL CLAIM: the press reaches the host, which is what draws the menu
                    // that has Remove in it. Recorded rather than shown, so no dialog takes the
                    // window and delivers ACTION_CANCEL underneath mid-test.
                    @Override public void onLongPress(Desk.Item item) { pressed[0] = item; }
                    @Override public void onLongPressEmpty() { }
                    @Override public void onSwipeUp() { }
                    @Override public void onChanged() { }
                    @Override public int minSpanX(Desk.Item item) { return 1; }
                    @Override public int minSpanY(Desk.Item item) { return 1; }
                    @Override public boolean resizable(Desk.Item item) { return false; }
                    @Override public void onResized(Desk.Item item, int cw, int ch) { }
                }, place.poster.app.ui.PcTheme.of("cyberpunk"));

                java.util.List<Desk.Item> items = new java.util.ArrayList<Desk.Item>();
                Desk.Item it = new Desk.Item(Desk.widgetKey(id), 0, 0, 2, 2);
                Desk.add(items, it, 4, 5);
                d.setGrid(4, 5);
                d.setItems(items);
                // Laid out by hand: it is never attached to a window, so nothing else can repaint
                // it, and cellW()/cellH() need a real size.
                int w = 800, h = 1000;
                d.measure(android.view.View.MeasureSpec.makeMeasureSpec(
                              w, android.view.View.MeasureSpec.EXACTLY),
                          android.view.View.MeasureSpec.makeMeasureSpec(
                              h, android.view.View.MeasureSpec.EXACTLY));
                d.layout(0, 0, w, h);
                clickable[0] = hasClickable(d);
                deskOut[0] = d;
            });

            final DeskView desk = deskOut[0];
            assertNotNull("no desk", desk);
            Log.i(TAG, "widget probe: clickable widget content present = " + clickable[0]);

            InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
                long t = android.os.SystemClock.uptimeMillis();
                desk.dispatchTouchEvent(android.view.MotionEvent.obtain(
                        t, t, android.view.MotionEvent.ACTION_DOWN,
                        desk.cellW() / 2f, desk.cellH() / 2f, 0));
            });
            Thread.sleep(800);                       // past the 400ms long-press

            final boolean[] lifted = new boolean[]{ false };
            InstrumentationRegistry.getInstrumentation().runOnMainSync(
                    () -> lifted[0] = desk.editingItem() != null);

            Log.i(TAG, "widget probe: long press -> lifted=" + lifted[0]
                    + " host.onLongPress=" + pressed[0] + " clickableContent=" + clickable[0]);
            assertNotNull("a long press on a placed widget never reached the desktop — its Remove,"
                    + " Resize and drag are all unreachable (clickableContent=" + clickable[0] + ")",
                    pressed[0]);
            assertTrue("the widget was not lifted, so it could not be dragged", lifted[0]);
            assertEquals("a different item was pressed", made[0], pressed[0].widgetId());
        } finally {
            s.close();
        }
    }

    private static boolean hasClickable(android.view.View v) {
        if (v.isClickable()) return true;
        if (!(v instanceof android.view.ViewGroup)) return false;
        android.view.ViewGroup g = (android.view.ViewGroup) v;
        for (int i = 0; i < g.getChildCount(); i++) if (hasClickable(g.getChildAt(i))) return true;
        return false;
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
