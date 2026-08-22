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
                    @Override public int maxSpanX(Desk.Item item) { return 4; }
                    @Override public int maxSpanY(Desk.Item item) { return 5; }
                    @Override public boolean resizable(Desk.Item item) { return false; }
                    @Override public void onResized(Desk.Item item, int cw, int ch) { }
                }, place.poster.app.ui.PcTheme.of("cyberpunk"));

                java.util.List<Desk.Item> items = new java.util.ArrayList<Desk.Item>();
                Desk.Item it = new Desk.Item(Desk.widgetKey(id), 0, 0, 2, 2);
                Desk.add(items, it, 4, 5);
                d.setGrid(4, 5);
                d.setItems(items);
                // IT IS ADDED TO THE WINDOW, and that is the whole difference between this test and
                // the previous version of it, which measured NOTHING and reported a product bug.
                //
                // `View.postDelayed` on a view with no AttachInfo does not post: it parks the
                // Runnable in the run queue that `dispatchAttachedToWindow` drains. DeskView arms
                // its long press with exactly that call, so on a hand-laid-out, never-attached view
                // the 400ms runnable never runs — at any delay, on any device. The last run said
                // `lifted=false host.onLongPress=null clickableContent=true`, which is the shape of
                // "the timer never fired" and was read as "the press never reached the desktop".
                //
                // Added with `addContentView` rather than into the launcher's own desk holder, so
                // HomeActivity's redraw (which finds R.id.pc_home_desk and calls setItems, clearing
                // `editing`) cannot arrive mid-gesture — the artefact the previous version was
                // written to escape. This one is attached AND out of that path.
                a.addContentView(d, new android.widget.FrameLayout.LayoutParams(800, 1000));
                deskOut[0] = d;
            });

            final DeskView desk = deskOut[0];
            assertNotNull("no desk", desk);

            // Wait for the attach and the layout the window will give it — measured, not slept at.
            boolean ready = false;
            for (int i = 0; i < 50 && !ready; i++) {
                final boolean[] ok = new boolean[]{ false };
                InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
                    ok[0] = desk.isAttachedToWindow() && desk.getWidth() > 0 && desk.getHeight() > 0;
                    if (ok[0]) clickable[0] = hasClickable(desk);
                });
                ready = ok[0];
                if (!ready) Thread.sleep(100);
            }
            // SAID SEPARATELY. A view that never attached cannot arm a long press, so reporting that
            // as "the press did not reach the desktop" is a test failure wearing a product bug's
            // clothes — which is exactly what happened for two runs.
            assertTrue("the DeskView under test never attached to a window, so its postDelayed"
                    + " long-press could not fire — this measures nothing about the product",
                    ready);
            Log.i(TAG, "widget probe: clickable widget content present = " + clickable[0]);

            InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
                long t = android.os.SystemClock.uptimeMillis();
                desk.dispatchTouchEvent(android.view.MotionEvent.obtain(
                        t, t, android.view.MotionEvent.ACTION_DOWN,
                        desk.cellW() / 2f, desk.cellH() / 2f, 0));
            });
            Thread.sleep(800);                       // past the 400ms long-press

            // THE LIFT COMES FIRST AND THE MENU COMES ON LIFT-OFF, and that order is the fix for
            // "moving a app is hard when that window pop hides where you want to put the app": the
            // menu is a dialog over the desktop, and opening it the instant the press fires covered
            // the cells the item was being dragged towards.
            final boolean[] lifted = new boolean[]{ false };
            InstrumentationRegistry.getInstrumentation().runOnMainSync(
                    () -> lifted[0] = desk.editingItem() != null);
            assertTrue("the widget was not lifted, so it could not be dragged"
                    + " (clickableContent=" + clickable[0] + ")", lifted[0]);
            assertTrue("the menu opened while the finger was still down, over the desktop the item"
                    + " is being dragged across", pressed[0] == null);

            InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
                long t = android.os.SystemClock.uptimeMillis();
                desk.dispatchTouchEvent(android.view.MotionEvent.obtain(
                        t, t, android.view.MotionEvent.ACTION_UP,
                        desk.cellW() / 2f, desk.cellH() / 2f, 0));
            });

            Log.i(TAG, "widget probe: long press -> lifted=" + lifted[0]
                    + " host.onLongPress=" + pressed[0] + " clickableContent=" + clickable[0]);
            assertNotNull("a long press on a placed widget never reached the desktop — its Remove,"
                    + " Resize and drag are all unreachable (clickableContent=" + clickable[0] + ")",
                    pressed[0]);
            assertEquals("a different item was pressed", made[0], pressed[0].widgetId());
        } finally {
            s.close();
        }
    }

    @Test
    public void aPlacedWidgetCanBeRESIZED() throws Exception {
        // "weather widget is too wide and i can't see the text for the city name, can't resize it
        // or nothing." The second half is a gesture that could never reach this view.
        //
        // `beginTouch` grabs a resize handle and returns BEFORE it arms a long press, so nothing
        // sets `stealing` — and the intercept used to return false for that DOWN, which left the
        // gesture with the widget's own RemoteViews. They consume it, so every MOVE went to them
        // and `resizeTo` was never called. An ICON is an inert view and its DOWN falls through to
        // onTouchEvent, so resizing worked on everything EXCEPT the one kind of item that has
        // handles.
        shell("appwidget grantbind --package " + ctx.getPackageName() + " --user 0");
        List<Widgets.Choice> rows = widgets.providers(200, 200);
        Widgets.Choice pick = null;
        for (Widgets.Choice c : rows) if (c.info.configure == null) { pick = c; break; }
        assertNotNull("nothing addable without a person", pick);

        final Widgets.Choice chosen = pick;
        final int[] made = new int[]{ -1 };
        HomeRoles.enableLauncherComponent(ctx, true);
        ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
        try {
            s.onActivity(a -> made[0] = widgets.add(a, chosen));
            assertTrue("could not place a widget to resize", made[0] >= 0);
            allocated.add(made[0]);

            final DeskView[] deskOut = new DeskView[1];
            s.onActivity(a -> {
                final int id = made[0];
                DeskView d = new DeskView(a);
                d.bind(new DeskView.Host() {
                    @Override public android.view.View viewFor(Desk.Item item) { return widgets.view(a, id); }
                    @Override public void onOpen(Desk.Item item) { }
                    @Override public void onLongPress(Desk.Item item) { }
                    @Override public void onLongPressEmpty() { }
                    @Override public void onSwipeUp() { }
                    @Override public void onChanged() { }
                    @Override public int minSpanX(Desk.Item item) { return 1; }
                    @Override public int minSpanY(Desk.Item item) { return 1; }
                    @Override public int maxSpanX(Desk.Item item) { return 4; }
                    @Override public int maxSpanY(Desk.Item item) { return 5; }
                    @Override public boolean resizable(Desk.Item item) { return true; }
                    @Override public void onResized(Desk.Item item, int cw, int ch) { }
                }, place.poster.app.ui.PcTheme.of("cyberpunk"));
                java.util.List<Desk.Item> items = new java.util.ArrayList<Desk.Item>();
                Desk.add(items, new Desk.Item(Desk.widgetKey(id), 0, 0, 2, 2), 4, 5);
                d.setGrid(4, 5);
                d.setItems(items);
                // ATTACHED. `View.postDelayed` on a view with no AttachInfo parks the Runnable until
                // it is attached, so the long press that lifts the item would never fire.
                a.addContentView(d, new android.widget.FrameLayout.LayoutParams(800, 1000));
                deskOut[0] = d;
            });
            final DeskView desk = deskOut[0];
            assertNotNull("no desk", desk);
            boolean ready = false;
            for (int i = 0; i < 50 && !ready; i++) {
                final boolean[] ok = new boolean[]{ false };
                InstrumentationRegistry.getInstrumentation().runOnMainSync(
                        () -> ok[0] = desk.isAttachedToWindow() && desk.getWidth() > 0);
                ready = ok[0];
                if (!ready) Thread.sleep(100);
            }
            assertTrue("the DeskView never attached, so nothing here measures the product", ready);

            // Lift it — a resize is only offered on an item that is already in edit mode.
            touch(desk, android.view.MotionEvent.ACTION_DOWN, 0.5f, 0.5f);
            Thread.sleep(800);
            touch(desk, android.view.MotionEvent.ACTION_UP, 0.5f, 0.5f);
            final boolean[] lifted = new boolean[]{ false };
            InstrumentationRegistry.getInstrumentation().runOnMainSync(
                    () -> lifted[0] = desk.editingItem() != null);
            assertTrue("the widget was never lifted, so its handles were never drawn", lifted[0]);

            // Now drag the RIGHT handle one cell out.
            touch(desk, android.view.MotionEvent.ACTION_DOWN, 2.0f, 1.0f);
            touch(desk, android.view.MotionEvent.ACTION_MOVE, 2.6f, 1.0f);
            touch(desk, android.view.MotionEvent.ACTION_MOVE, 3.0f, 1.0f);
            touch(desk, android.view.MotionEvent.ACTION_UP, 3.0f, 1.0f);

            final int[] span = new int[]{ -1 };
            InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
                Desk.Item it = Desk.at(desk.items(), 0, 0);
                span[0] = it == null ? -1 : it.spanX;
            });
            Log.i(TAG, "widget probe: resize right edge -> spanX=" + span[0]);
            assertEquals("dragging a placed widget's resize handle changed nothing — the gesture"
                    + " never reached the desktop, so a widget can be placed and never adjusted",
                    3, span[0]);
        } finally {
            s.close();
        }
    }

    @Test
    public void onAPhoneAnAlreadyGiganticWidgetIsBroughtBackINSIDEItsOwnCeiling() throws Exception {
        // "the weather widget is just too gigantic on phones", still, after the arithmetic that
        // produced the giant span was fixed — because a PLACED widget is stored with a SPAN and
        // nothing re-derives it. The old density-inflated maths made a 180dp card ask for six of a
        // four-column grid, which the cap turned into the full width, and there it stayed on every
        // draw of every later build. The only way out was to remove it.
        //
        // So a stored span is put back inside `maxResizeWidth`/`maxResizeHeight` — the widget's own
        // statement of how big it wants to get. Written straight into the arrangement here, exactly
        // as the buggy build left it, and then the launcher is asked to draw.
        shell("appwidget grantbind --package " + ctx.getPackageName() + " --user 0");
        Widgets.Choice pick = null;
        String mine = ctx.getPackageName();
        for (Widgets.Choice c : widgets.providers(200, 200)) {
            if (c.info.configure != null) continue;
            if (!mine.equals(c.info.provider.getPackageName())) continue;
            if (c.info.maxResizeWidth > 0) { pick = c; break; }
        }
        // A SKIP WITH ITS REASON, never a pass: on an image where none of ours declares a ceiling
        // there is nothing here to measure.
        org.junit.Assume.assumeTrue("no widget of ours declares maxResizeWidth on this image",
                pick != null);

        HomeRoles.enableLauncherComponent(ctx, true);
        asAPhone();
        LauncherPrefs prefs = new LauncherPrefs(ctx);
        String geom = "";
        String before = "";
        try {
            int[] g = deskShape();
            assertEquals("a phone did not get the phone grid", 4, g[0]);
            geom = HomeMetrics.geometry(g[0], g[1]);
            before = prefs.desk(geom);

            final Widgets.Choice chosen = pick;
            final int[] made = new int[]{ -1 };
            ActivityScenario<HomeActivity> mk = ActivityScenario.launch(HomeActivity.class);
            try { mk.onActivity(a -> made[0] = widgets.add(a, chosen)); } finally { mk.close(); }
            assertTrue("the widget was not bound", made[0] >= 0);
            allocated.add(made[0]);

            // PIXELS, like every other size here — the device printed `ceiling=715x550` for a
            // manifest saying `260dp` at density 2.75. What is asserted below is the user-visible
            // claim rather than the arithmetic: a widget stored at the full width does not stay
            // there.
            int ceilingCells = Math.max(1, chosen.info.maxResizeWidth / g[2]);
            Log.i(TAG, "phone widgets: " + chosen.info.provider.getShortClassName()
                    + " ceiling=" + chosen.info.maxResizeWidth + "px cell=" + g[2]
                    + "px -> " + ceilingCells + " cells of " + g[0]);
            org.junit.Assume.assumeTrue("its ceiling is the whole grid here, so there is nothing to"
                    + " bring back", ceilingCells < g[0]);

            // FULL WIDTH, which is what the old arithmetic always ended at.
            List<Desk.Item> items = new ArrayList<Desk.Item>();
            items.add(new Desk.Item(Desk.widgetKey(made[0]), 0, 0, g[0], 2));
            prefs.setDesk(geom, Desk.serialize(items));

            ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
            try { Thread.sleep(1500); } finally { s.close(); }

            String after = prefs.desk(geom);
            Log.i(TAG, "phone widgets: gigantic " + g[0] + "-wide widget redrew as -> "
                    + after.replace('\n', ' ') + " (ceiling " + ceilingCells + " cells)");
            Desk.Item back = Desk.byKey(Desk.parse(after), Desk.widgetKey(made[0]));
            assertNotNull("the widget was dropped rather than resized: " + after, back);
            assertTrue("a widget stored at the full width of the grid stayed there — its provider"
                    + " says it wants at most " + ceilingCells + " cells. desktop=" + after,
                    back.spanX <= ceilingCells);
        } finally {
            if (!geom.isEmpty()) prefs.setDesk(geom, before);
            asItWas();
        }
    }

    @Test
    public void aPlacedWidgetCanBeREMOVED() throws Exception {
        // "i can't remove widgets". Remove matched the desktop by OBJECT IDENTITY, and this activity
        // rebuilds `desk.items()` from stored preferences on every redraw — including the one it
        // runs after its own layout. So the item the menu was opened about is routinely no longer
        // the object on the desk, `List.remove(Object)` matches nothing, and the menu closes exactly
        // as if it had worked.
        //
        // Driven with a DELIBERATELY STALE item: same key, different object, which is precisely what
        // a redraw between the long press and the tap leaves behind.
        shell("appwidget grantbind --package " + ctx.getPackageName() + " --user 0");
        List<Widgets.Choice> rows = widgets.providers(200, 200);
        Widgets.Choice pick = null;
        for (Widgets.Choice c : rows) if (c.info.configure == null) { pick = c; break; }
        assertNotNull("nothing addable without a person", pick);

        HomeRoles.enableLauncherComponent(ctx, true);
        LauncherPrefs prefs = new LauncherPrefs(ctx);
        int[] shape = deskShape();
        String geometry = HomeMetrics.geometry(shape[0], shape[1]);
        String savedDesk = prefs.desk(geometry);
        prefs.setDesk(geometry, "");
        final Widgets.Choice chosen = pick;
        final int[] made = new int[]{ -1 };
        final String[] after = new String[]{ "" };
        final String[] persistedAfter = new String[]{ "" };
        ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
        try {
            Thread.sleep(1200);
            s.onActivity(a -> made[0] = widgets.add(a, chosen));
            assertTrue("the widget was not bound", made[0] >= 0);
            allocated.add(made[0]);
            s.onActivity(a -> a.placeWidget(made[0]));
            Thread.sleep(400);

            final String[] before = new String[]{ "" };
            s.onActivity(a -> before[0] = Desk.serialize(a.deskItemsForTest()));
            assertTrue("the widget was not on the desktop to begin with, so removing it proves"
                    + " nothing: " + before[0], before[0].contains(Desk.widgetKey(made[0])));

            s.onActivity(a -> {
                // A fresh object with the same key — never the one the desk is holding.
                a.removeFromDesk(new Desk.Item(Desk.widgetKey(made[0]), 0, 0, 1, 1));
                after[0] = Desk.serialize(a.deskItemsForTest());
            });
            persistedAfter[0] = prefs.desk(geometry);
        } finally {
            s.close();
            prefs.setDesk(geometry, savedDesk);
        }
        Log.i(TAG, "widget probe: after remove the desktop is " + after[0].replace('\n', ' '));
        assertFalse("Remove left the widget on the home screen — it matched by object identity and"
                + " the desk had already been rebuilt. desktop=" + after[0],
                after[0].contains(Desk.widgetKey(made[0])));
        assertFalse("it did not survive a redraw either",
                persistedAfter[0].contains(Desk.widgetKey(made[0])));
    }

    /** One touch event at a point given in CELLS, dispatched on the main thread. */
    private void touch(final DeskView d, final int action, final float cx, final float cy) {
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
            long t = android.os.SystemClock.uptimeMillis();
            d.dispatchTouchEvent(android.view.MotionEvent.obtain(
                    t, t, action, d.cellW() * cx, d.cellH() * cy, 0));
        });
    }

    private static boolean hasClickable(android.view.View v) {
        if (v.isClickable()) return true;
        if (!(v instanceof android.view.ViewGroup)) return false;
        android.view.ViewGroup g = (android.view.ViewGroup) v;
        for (int i = 0; i < g.getChildCount(); i++) if (hasClickable(g.getChildAt(i))) return true;
        return false;
    }

    // ------------------------------------------------------------------ a phone, not a tablet

    /** A real phone: 1080x2340 at 440dpi is a short side of 393dp, which is the phone path. */
    private void asAPhone() throws Exception {
        shell("wm size 1080x2340");
        shell("wm density 440");
        Thread.sleep(2500);
    }

    private void asItWas() throws Exception {
        // UNCONDITIONALLY, in a finally. A device left resized poisons every test after it on this
        // boot — the same rule the tablet case had to learn.
        shell("wm size reset");
        shell("wm density reset");
        Thread.sleep(2500);
    }

    /** cols, rows, cellW px, cellH px — read off the launcher's own DeskView, not computed. */
    private int[] deskShape() {
        final int[] out = new int[]{ -1, -1, -1, -1 };
        HomeRoles.enableLauncherComponent(ctx, true);
        ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
        try {
            s.onActivity(a -> {
                android.view.ViewGroup h =
                        (android.view.ViewGroup) a.findViewById(place.poster.app.R.id.pc_home_desk);
                DeskView d = (DeskView) h.getChildAt(0);
                out[0] = d.cols(); out[1] = d.rows(); out[2] = d.cellW(); out[3] = d.cellH();
            });
        } finally {
            s.close();
        }
        return out;
    }

    @Test
    public void onAPhoneOurOwnWidgetsGetAtLeastTheSizeTheyASKEDFOR() throws Exception {
        // "widgets need support to fit on mobile phone screen" / "widgets look great on tablet".
        //
        // That is one sentence twice: the tablet grid is 5-7 columns by 6-8 rows and a phone's is 4
        // by 3-6, so the same widget asking for the same rectangle finds room on one and not the
        // other — and the tablet is the only size that was ever measured on a device. This is the
        // phone half, same technique (`wm size` + `wm density` reshape the running system), so the
        // launcher takes the phone path through the configuration change a real rotation delivers.
        //
        // WHAT IS ASSERTED IS OUR OWN THREE, and what is LOGGED is every provider on the image. A
        // third-party widget demanding more than a phone grid can give is a fact about that widget;
        // ours failing to fit on a phone is a fact about us.
        HomeRoles.enableLauncherComponent(ctx, true);
        asAPhone();
        try {
            int sw = ctx.getResources().getConfiguration().smallestScreenWidthDp;
            assertTrue("the resize did not take: smallestScreenWidthDp is " + sw, sw < 600);
            int[] g = deskShape();
            assertEquals("a phone did not get the phone grid", 4, g[0]);
            assertTrue("no rows were measured: " + g[1], g[1] >= 3);
            assertTrue("the desk was never laid out: cell " + g[2] + "x" + g[3], g[2] > 0 && g[3] > 0);

            // EVERYTHING HERE IS PIXELS. `AppWidgetProviderInfo.minWidth` and friends are resolved
            // against the display density by the platform — a manifest saying 250dp reads back as
            // 688 on a 440dpi phone — and mixing them with a cell measured in dp is what multiplied
            // every widget's demand by the density. That is the bug this test found: the first run
            // printed "MusicWidget needs 688x110dp, a phone cell gives it 376x186".
            float density = ctx.getResources().getDisplayMetrics().density;
            Log.i(TAG, "phone widgets: sw=" + sw + "dp density=" + density
                    + " grid=" + g[0] + "x" + g[1] + " cell=" + g[2] + "x" + g[3] + "px");

            String mine = ctx.getPackageName();
            List<String> tooBig = new ArrayList<String>();
            int ours = 0;
            for (Widgets.Choice c : widgets.providers(g[2], g[3])) {
                AppWidgetProviderInfo i = c.info;
                int floorW = i.minResizeWidth > 0 ? i.minResizeWidth : i.minWidth;
                int floorH = i.minResizeHeight > 0 ? i.minResizeHeight : i.minHeight;
                // THE ONLY QUESTION THAT MATTERS: does the SMALLEST shape this widget will accept
                // fit on a phone's grid at all? If it needs more columns than the grid has, no free
                // rectangle of that shape can ever exist and the answer is always "no room".
                int needX = Widgets.spanFor(floorW, g[2]);
                int needY = Widgets.spanFor(floorH, g[3]);
                boolean fits = needX <= g[0] && needY <= g[1];
                boolean isOurs = mine.equals(i.provider.getPackageName());
                Log.i(TAG, "phone widgets: " + (isOurs ? "OURS " : "     ")
                        + i.provider.flattenToShortString()
                        + " min=" + i.minWidth + "x" + i.minHeight + "px"
                        + " floor=" + floorW + "x" + floorH + "px"
                        // RAW, and labelled raw. minWidth/minResizeWidth are density-resolved by
                        // the platform; maxResizeWidth (API 31) comes back as the manifest's dp.
                        // Printed side by side so that stays a measurement, not a memory.
                        + " ceiling=" + i.maxResizeWidth + "x" + i.maxResizeHeight + "(raw)"
                        + " -> smallest " + needX + "x" + needY + " cells of " + g[0] + "x" + g[1]
                        + (fits ? "" : "  DOES NOT FIT"));
                if (!isOurs) continue;
                ours++;
                if (!fits) tooBig.add(i.provider.getShortClassName() + " needs at least "
                        + needX + "x" + needY + " cells of a " + g[0] + "x" + g[1] + " phone grid");
            }
            assertTrue("none of our own widget providers was listed at all", ours > 0);
            assertTrue("one of OUR widgets cannot fit a phone's home screen at any size it will"
                    + " accept, so adding it can only ever answer \"No room left on the home"
                    + " screen\": " + tooBig, tooBig.isEmpty());
        } finally {
            asItWas();
        }
        assertEquals("the phone grid did not come back", 4, deskShape()[0]);
    }

    @Test
    public void onAPhoneAWidgetCanActuallyBeADDED() throws Exception {
        // "still can't add widget to phone" — the string being seen is `home_desktop_full`, so the
        // widget is being REFUSED A PLACE, and no amount of listing or previewing proves otherwise.
        // This drives the launcher's real `placeWidget` on a real phone-sized grid with a real
        // seeded desktop, and asks the only question the report asks: is it on the desk afterwards?
        shell("appwidget grantbind --package " + ctx.getPackageName() + " --user 0");
        List<Widgets.Choice> rows = widgets.providers(200, 200);
        Widgets.Choice pick = null;
        String mine = ctx.getPackageName();
        // OURS BY PREFERENCE — a third-party widget demanding more than a phone can give is a fact
        // about that widget; ours failing to fit is a fact about us.
        for (Widgets.Choice c : rows) {
            if (c.info.configure != null) continue;
            if (mine.equals(c.info.provider.getPackageName())) { pick = c; break; }
            if (pick == null) pick = c;
        }
        assertNotNull("nothing addable without a person", pick);

        HomeRoles.enableLauncherComponent(ctx, true);
        asAPhone();
        LauncherPrefs prefs = new LauncherPrefs(ctx);
        String geom = "";
        String before = "";
        try {
            int[] g = deskShape();
            assertEquals("a phone did not get the phone grid", 4, g[0]);
            geom = HomeMetrics.geometry(g[0], g[1]);
            before = prefs.desk(geom);

            // A DESKTOP LIKE A REAL ONE: the default tiles across the first two rows, which is what
            // `seedHome` leaves and what the person adding a widget is looking at.
            List<Desk.Item> seed = new ArrayList<Desk.Item>();
            HomeTiles.Tile[] cat = HomeTiles.catalogue();
            for (int n = 0; n < 8 && n < cat.length; n++) {
                seed.add(new Desk.Item("pc:" + cat[n].view, n % g[0], n / g[0], 1, 1));
            }
            prefs.setDesk(geom, Desk.serialize(seed));

            final Widgets.Choice chosen = pick;
            final int[] made = new int[]{ -1 };
            final String[] landed = new String[]{ "" };
            ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
            try {
                Thread.sleep(1200);                       // the app scan and the first layout
                s.onActivity(a -> made[0] = widgets.add(a, chosen));
                assertTrue("the widget was not bound", made[0] >= 0);
                allocated.add(made[0]);
                s.onActivity(a -> a.placeWidget(made[0]));
                Thread.sleep(400);
                s.onActivity(a -> landed[0] = Desk.serialize(a.deskItemsForTest()));
            } finally {
                s.close();
            }
            Log.i(TAG, "phone widgets: after placeWidget the desktop is "
                    + landed[0].replace('\n', ' '));
            assertTrue("a widget could not be added to a phone's home screen at all — this is the"
                    + " \"No room left on the home screen\" report, measured. desktop=" + landed[0],
                    landed[0].contains(Desk.widgetKey(made[0])));
        } finally {
            if (!geom.isEmpty()) prefs.setDesk(geom, before);
            asItWas();
        }
    }

    @Test
    public void onAPhoneAWidgetWithNoRoomIsKEPTRatherThanDeleted() throws Exception {
        // THE THING THAT MAKES "IT DOES NOT FIT" UNRECOVERABLE. `Desk.fit` hands back what it could
        // not place and `redrawDesk` used not to carry it forward — it saved the SHORTENED
        // arrangement, so a widget that lost its room on a smaller grid was deleted from the desktop
        // for good, with its id still bound to a widget nobody could see and nothing said. A phone
        // grid is where a desktop runs out of room, which is why this is measured at phone size.
        shell("appwidget grantbind --package " + ctx.getPackageName() + " --user 0");
        List<Widgets.Choice> rows = widgets.providers(90, 90);
        Widgets.Choice pick = null;
        for (Widgets.Choice c : rows) if (c.info.configure == null) { pick = c; break; }
        assertNotNull("nothing addable without a person", pick);

        HomeRoles.enableLauncherComponent(ctx, true);
        asAPhone();
        LauncherPrefs prefs = new LauncherPrefs(ctx);
        String geom = "";
        String before = "";
        try {
            int[] g = deskShape();
            assertEquals("a phone did not get the phone grid", 4, g[0]);
            geom = HomeMetrics.geometry(g[0], g[1]);
            before = prefs.desk(geom);

            final int[] made = new int[]{ -1 };
            ActivityScenario<HomeActivity> mk = ActivityScenario.launch(HomeActivity.class);
            final Widgets.Choice chosen = pick;
            try { mk.onActivity(a -> made[0] = widgets.add(a, chosen)); } finally { mk.close(); }
            assertTrue("the widget was not bound", made[0] >= 0);
            allocated.add(made[0]);

            // EVERY CELL TAKEN by tiles that really resolve (so the uninstalled-app sweep cannot
            // remove them), plus the widget with nowhere to go.
            List<Desk.Item> items = new ArrayList<Desk.Item>();
            HomeTiles.Tile[] cat = HomeTiles.catalogue();
            int n = 0;
            for (int r = 0; r < g[1]; r++) {
                for (int c = 0; c < g[0] && n < cat.length; c++) {
                    items.add(new Desk.Item("pc:" + cat[n++].view, c, r, 1, 1));
                }
            }
            assertTrue("the catalogue is too short to fill a phone desktop", n >= g[0] * g[1]);
            items.add(new Desk.Item(Desk.widgetKey(made[0]), 0, 0, 2, 2));
            prefs.setDesk(geom, Desk.serialize(items));

            // Draw it — this is redrawDesk running for real on the arrangement above.
            ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
            try { Thread.sleep(800); } finally { s.close(); }

            String saved = prefs.desk(geom);
            Log.i(TAG, "phone widgets: full-desk redraw kept -> " + saved.replace('\n', ' '));
            assertTrue("a widget with no room was DELETED from the saved desktop — it cannot be got"
                    + " back and its id is still bound to a widget nobody can see. saved=" + saved,
                    saved.contains(Desk.widgetKey(made[0])));
            assertNotNull("the widget id was released while the item was still on the desktop",
                    widgets.infoOf(made[0]));
        } finally {
            if (!geom.isEmpty()) prefs.setDesk(geom, before);
            asItWas();
        }
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
