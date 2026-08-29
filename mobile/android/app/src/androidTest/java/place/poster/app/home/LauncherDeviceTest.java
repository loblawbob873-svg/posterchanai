package place.poster.app.home;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.app.Instrumentation;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;
import android.widget.GridView;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.util.List;

import place.poster.app.MainActivity;
import place.poster.app.ui.PcTheme;
import place.poster.app.ui.PcThemeStore;

/**
 * THE LAUNCHER, ON A REAL ANDROID.
 *
 * Everything native in this app used to be guarded by matching TEXT in a Java source, because there
 * was no device in the loop. A launcher cannot be checked that way at all: `queryIntentActivities`,
 * `RoleManager`, the package-manager component switch and the wallpaper window all only exist on a
 * device, and the one thing that matters here — that the home screen keeps working when the rest of
 * the app does not — is a claim about a running system.
 *
 * These run on the emulator in .github/workflows/android-emulator.yml. They are deliberately
 * READ-MOSTLY: the one thing they change (the home component's enabled flag) is put back in @After,
 * because leaving it on would mean the next test in the same boot inherits a phone that offers a
 * home-screen chooser.
 */
@RunWith(AndroidJUnit4.class)
public class LauncherDeviceTest {

    private Context ctx;
    private boolean wasEnabled;

    @Before
    public void setUp() {
        ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        wasEnabled = HomeRoles.launcherComponentEnabled(ctx);
    }

    @After
    public void tearDown() {
        HomeRoles.enableLauncherComponent(ctx, wasEnabled);
    }

    @Test
    public void theHomeComponentShipsDisabled() throws Exception {
        // OPT-IN, MEASURED. A CATEGORY_HOME activity makes Android offer this app in "Select a Home
        // app" from the moment it is installed; shipping the component disabled is what makes the
        // opt-in true rather than nearly true.
        //
        // Read from the INSTALLED MANIFEST (MATCH_DISABLED_COMPONENTS is what makes a disabled
        // component visible to the query at all), never from the component's current state:
        // scripts/android_device_checks.sh runs first on the same boot and switches it on to press
        // HOME, so a state check here would fail for a reason that has nothing to do with the rule.
        android.content.pm.ActivityInfo info = ctx.getPackageManager().getActivityInfo(
                new ComponentName(ctx, HomeActivity.class),
                android.content.pm.PackageManager.MATCH_DISABLED_COMPONENTS);
        assertNotNull("the launcher is not installed at all", info);
        assertFalse("the launcher must not be offered until somebody asks for it", info.enabled);
    }

    @Test
    public void launcherAndMobileAppOwnDifferentTasks() throws Exception {
        /* A launcher is the phone's resting task, not the root of PosterChan's WebView task. Tiles
         * start MainActivity with NEW_TASK; a shared affinity lets Android put that activity above
         * HomeActivity, so HOME has to clear/reshape the same task and Recents cannot independently
         * restore the app. Read the installed manifest because task affinity is platform routing,
         * not a property either Activity can faithfully simulate. */
        android.content.pm.PackageManager pm = ctx.getPackageManager();
        int flags = android.content.pm.PackageManager.MATCH_DISABLED_COMPONENTS;
        android.content.pm.ActivityInfo home = pm.getActivityInfo(
                new ComponentName(ctx, HomeActivity.class), flags);
        android.content.pm.ActivityInfo app = pm.getActivityInfo(
                new ComponentName(ctx, MainActivity.class), flags);
        assertNotNull(home.taskAffinity);
        assertNotNull(app.taskAffinity);
        assertFalse("launcher and WebView app must remain independently resumable",
                home.taskAffinity.equals(app.taskAffinity));
    }

    @Test
    public void thePhoneListsItsOwnApps() {
        List<AppShelf.Entry> found = new AppRepo(ctx).installed();
        assertTrue("no launchable apps found — the package query or <queries> is wrong",
                found.size() > 0);
        for (AppShelf.Entry e : found) {
            assertFalse("the launcher must not list itself", ctx.getPackageName().equals(e.pkg));
        }
    }

    @Test
    public void thereIsAWayToSettingsEvenWithNothingInstalled() {
        // The escape hatch, checked against the real arrangement rather than against a fixture.
        List<AppShelf.Entry> rows = AppShelf.arrange(
                new java.util.ArrayList<AppShelf.Entry>(), HomeTiles.ours(),
                null, null, "");
        boolean found = false;
        for (AppShelf.Entry e : rows) if (HomeTiles.VIEW_SETTINGS.equals(e.view)) found = true;
        assertTrue("no route to the phone's Settings", found);
        // And the intent it fires must actually resolve on this device.
        assertNotNull(ctx.getPackageManager().resolveActivity(
                new Intent(android.provider.Settings.ACTION_SETTINGS), 0));
    }

    @Test
    public void theLauncherCanSeeAnotherHomeApp() {
        // Without this, HomeRoles.releaseHome refuses for ever and the home screen can never be
        // handed back — which is the brick the whole feature is written to avoid. It is a package
        // VISIBILITY question (Android 11+), so only a device can answer it.
        assertTrue("no other home app visible — check the MAIN/HOME <queries> entry",
                new AppRepo(ctx).anotherHomeExists());
    }

    @Test
    public void theHomeScreenDrawsWithoutAWebViewAnywhere() {
        // THE SAFETY ARGUMENT, ON THE DEVICE. A launcher that fails takes the phone's home screen
        // with it, and this app's WebView renderer is measured to die under memory pressure. So the
        // home screen must contain no browser engine at all — not a hidden one, not a parked one.
        HomeRoles.enableLauncherComponent(ctx, true);
        ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
        try {
            s.onActivity(a -> {
                View root = a.findViewById(android.R.id.content);
                assertNotNull(root);
                assertEquals("the home screen must not host a WebView", 0, countWebViews(root));
                GridView g = a.findViewById(place.poster.app.R.id.pc_home_grid);
                assertNotNull("no grid", g);
                assertTrue("the grid drew nothing", g.getAdapter().getCount() > 0);
            });
        } finally {
            s.close();
        }
    }

    @Test
    public void backDoesNotLeaveThePhoneWithNoHomeScreen() {
        HomeRoles.enableLauncherComponent(ctx, true);
        ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
        try {
            s.onActivity(a -> {
                a.onBackPressed();
                assertFalse("back finished the home screen", a.isFinishing());
            });
        } finally {
            s.close();
        }
    }

    @Test
    public void onStartOnlyHomeRestoreClosesTransientLauncherOverlays() throws Exception {
        /* Several OEMs resume HOME without onNewIntent. Open the real drawer, background the real
         * activity, and resume it through lifecycle alone: it must return to the desktop. */
        HomeRoles.enableLauncherComponent(ctx, true);
        ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
        try {
            s.onActivity(a -> {
                try {
                    java.lang.reflect.Method open = HomeActivity.class.getDeclaredMethod("openDrawer");
                    open.setAccessible(true);
                    open.invoke(a);
                } catch (Exception e) { throw new RuntimeException(e); }
                View drawer = a.findViewById(place.poster.app.R.id.pc_home_drawer);
                assertEquals(View.VISIBLE, drawer.getVisibility());
            });
            s.moveToState(androidx.lifecycle.Lifecycle.State.CREATED);
            s.moveToState(androidx.lifecycle.Lifecycle.State.RESUMED);
            s.onActivity(a -> assertEquals("onStart-only HOME restore left the app drawer open",
                    View.GONE, a.findViewById(place.poster.app.R.id.pc_home_drawer).getVisibility()));
        } finally {
            s.close();
        }
    }

    @Test
    public void oneHomeLifecyclePairDoesNotReopenPosterChanButTwoIntentsDo() throws Exception {
        /* A real OEM reported one physical HOME as onStart -> onNewIntent. Counting both callbacks
         * reopened MainActivity immediately, so sending PosterChan to the background appeared to
         * replace the launcher with PosterChan. Exercise the real Activity/Looper here: the pending
         * onStart count must be cancelled by its paired intent, while an onStart-only first press
         * followed later by a second intent must still activate the double-HOME shortcut. */
        HomeRoles.enableLauncherComponent(ctx, true);
        Instrumentation inst = InstrumentationRegistry.getInstrumentation();
        /* Block the start at Instrumentation. MainActivity is singleTask and is normally reused, so
         * waiting for a newly-created Activity falsely reports that openApp did nothing on a warm
         * process. Monitor hits count the actual explicit start in both cold and warm cases. */
        Instrumentation.ActivityMonitor web = inst.addMonitor(
                MainActivity.class.getName(),
                new Instrumentation.ActivityResult(android.app.Activity.RESULT_CANCELED, null),
                true);
        Intent launch = new Intent(ctx, HomeActivity.class).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        HomeActivity homeActivity = (HomeActivity) inst.startActivitySync(launch);
        inst.waitForIdleSync();
        try {
            HomeDoublePress.clear();
            inst.runOnMainSync(() -> {
                homeActivity.getWindow().getDecorView().dispatchWindowFocusChanged(false);
                homeActivity.onStop();
                homeActivity.onStart();
                homeActivity.onNewIntent(
                        new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME));
            });
            Thread.sleep(500);
            assertEquals("one HOME lifecycle pair reopened PosterChan", 0, web.getHits());

            HomeDoublePress.clear();
            inst.runOnMainSync(() -> {
                homeActivity.getWindow().getDecorView().dispatchWindowFocusChanged(false);
                homeActivity.onStop();
                homeActivity.onStart();
            });
            Thread.sleep(250); // beyond HOME_START_ECHO_MS: the first physical press is now counted
            inst.runOnMainSync(() -> {
                homeActivity.getWindow().getDecorView().dispatchWindowFocusChanged(true);
                homeActivity.onNewIntent(
                        new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME));
            });
            Thread.sleep(300);
            assertEquals("an onStart-only first HOME plus a second HOME did not open the feed",
                    1, web.getHits());

            HomeDoublePress.clear();
            inst.runOnMainSync(() -> {
                homeActivity.getWindow().getDecorView().dispatchWindowFocusChanged(false);
                homeActivity.onStop();
                homeActivity.onStart();
                homeActivity.getWindow().getDecorView().dispatchWindowFocusChanged(true);
                homeActivity.onNewIntent(
                        new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_HOME));
            });
            Thread.sleep(300);
            assertEquals("a focused second HOME inside the debounce window was discarded",
                    2, web.getHits());
        } finally {
            inst.removeMonitor(web);
            inst.runOnMainSync(homeActivity::finish);
            inst.waitForIdleSync();
            HomeDoublePress.clear();
        }
    }

    @Test
    public void everyThemeDrawsSomething() {
        // Nine palettes, applied for real. A theme whose page drawable throws would take the home
        // screen down on the one phone that had it selected — which is the sort of bug that only
        // ever appears in somebody else's screenshot.
        HomeRoles.enableLauncherComponent(ctx, true);
        String before = PcThemeStore.slug(ctx);
        try {
            for (final String slug : PcTheme.SLUGS) {
                PcThemeStore.remember(ctx, slug);
                ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
                try {
                    s.onActivity(a -> {
                        View root = a.findViewById(place.poster.app.R.id.pc_home_root);
                        assertNotNull(slug + ": no root", root);
                        assertNotNull(slug + ": nothing painted", root.getBackground());
                    });
                } finally {
                    s.close();
                }
            }
        } finally {
            PcThemeStore.remember(ctx, before);
        }
    }

    @Test
    public void everyTileIconActuallyDRAWSSOMETHING() {
        // THE CHECK THAT WOULD HAVE CAUGHT "a lot of the PosterChan apps are empty circles".
        //
        // Every earlier check asked whether the resource EXISTS. All of them passed while the icons
        // rendered nothing: a VectorDrawable carrying a baked android:tint, tinted again at runtime
        // with setColorFilter, inflates fine, reports a size, and paints no pixels. The only question
        // that separates the two is whether anything is on the canvas — so this draws each one and
        // counts.
        HomeTiles.Tile[] all = HomeTiles.catalogue();
        // EVERY TILE, AND THE COUNT IS ASSERTED. A catalogue that shrank would quietly reduce this
        // audit to a sample of itself while still reporting a pass.
        assertTrue("the catalogue has shrunk to " + all.length + " — this audit is meant to cover"
                + " every tile on the home screen", all.length >= 40);
        // AT THE SIZES IT IS ACTUALLY DRAWN AT, not one comfortable one. A stroked vector can carry
        // a width that rounds to nothing at a small bound and looks perfect at a large one, which is
        // the same class of bug as the packed arc flags: fine everywhere it is easy to look.
        int[] sizes = { 32, 48, 96 };
        for (HomeTiles.Tile t : all) {
            int res = TileIcons.of(t.icon);
            assertTrue("no drawable for tile " + t.view + " (" + t.icon + ")", res != 0);
            for (int size : sizes) {
                android.graphics.drawable.Drawable d =
                        place.poster.app.ui.Skin.icon(ctx, res, 0xFFFFFFFF);
                assertNotNull("could not load " + t.icon, d);
                android.graphics.Bitmap bmp = android.graphics.Bitmap.createBitmap(
                        size, size, android.graphics.Bitmap.Config.ARGB_8888);
                android.graphics.Canvas canvas = new android.graphics.Canvas(bmp);
                d.setBounds(0, 0, size, size);
                d.draw(canvas);
                int lit = 0;
                for (int x = 0; x < size; x += 2) {
                    for (int y = 0; y < size; y += 2) {
                        if (android.graphics.Color.alpha(bmp.getPixel(x, y)) > 24) lit++;
                    }
                }
                bmp.recycle();
                assertTrue(t.icon + " drew nothing at " + size + "px — an empty circle on the home"
                        + " screen", lit > 2);
            }
        }
    }

    @Test
    public void everyTileIconDrawsInEveryThemesAccent() {
        // THE SECOND CAUSE, IF THERE IS ONE. The bug before the arc flags was a vector carrying a
        // baked android:tint that was then tinted AGAIN at runtime: it inflated, reported a size,
        // and painted nothing. That interaction depends on the colour, so an audit in one colour can
        // pass while a palette somebody actually uses draws blanks. Nine accents, every tile.
        for (String slug : PcTheme.SLUGS) {
            PcTheme.Palette pal = PcTheme.of(slug);
            for (HomeTiles.Tile t : HomeTiles.catalogue()) {
                android.graphics.drawable.Drawable d =
                        place.poster.app.ui.Skin.icon(ctx, TileIcons.of(t.icon), pal.accent);
                assertNotNull(slug + ": could not load " + t.icon, d);
                android.graphics.Bitmap bmp = android.graphics.Bitmap.createBitmap(
                        48, 48, android.graphics.Bitmap.Config.ARGB_8888);
                d.setBounds(0, 0, 48, 48);
                d.draw(new android.graphics.Canvas(bmp));
                int lit = 0;
                for (int x = 0; x < 48; x += 2) for (int y = 0; y < 48; y += 2) {
                    if (android.graphics.Color.alpha(bmp.getPixel(x, y)) > 24) lit++;
                }
                bmp.recycle();
                assertTrue(slug + ": " + t.icon + " drew nothing in this palette's accent", lit > 2);
            }
        }
    }

    @Test
    public void aTileWithNoIconStillShowsSomething() {
        // A coloured circle with nothing in it is indistinguishable from a broken launcher. The
        // fallback identifies the app instead of identifying a bug.
        android.graphics.drawable.Drawable d = place.poster.app.ui.Skin.letter(
                ctx, place.poster.app.ui.PcTheme.of("cyberpunk"), "Notes");
        assertNotNull(d);
        android.graphics.Bitmap bmp = android.graphics.Bitmap.createBitmap(
                64, 64, android.graphics.Bitmap.Config.ARGB_8888);
        d.setBounds(0, 0, 64, 64);
        d.draw(new android.graphics.Canvas(bmp));
        int lit = 0;
        for (int x = 0; x < 64; x += 2) for (int y = 0; y < 64; y += 2) {
            if (android.graphics.Color.alpha(bmp.getPixel(x, y)) > 24) lit++;
        }
        bmp.recycle();
        assertTrue("the fallback drew nothing either", lit > 4);
    }

    @Test
    public void everyTileIconResolvesAndInflates() {
        // A tile whose icon does not resolve draws a blank square with a label under it, which reads
        // as a broken app rather than as a missing file.
        for (HomeTiles.Tile t : HomeTiles.catalogue()) {
            int res = TileIcons.of(t.icon);
            assertTrue("no drawable for tile " + t.view + " (" + t.icon + ")", res != 0);
            assertNotNull("could not inflate " + t.icon, ctx.getResources().getDrawable(res, null));
        }
    }

    @Test
    public void onATabletTheGridIsWiderAndTheDockIsLonger() throws Exception {
        // "the launcher needs to work on tablet mode too", measured on the device rather than in
        // arithmetic. `wm size` + `wm density` reshape the running system: 2560x1600 at 240dpi has a
        // short side of 1066dp, which Android reports as a large screen, and the launcher takes the
        // tablet path through the same configuration change a real rotation delivers.
        //
        // IT LIVES HERE RATHER THAN IN android_device_checks.sh because that script cannot enable
        // the home component on this image at all (`pm enable` prints nothing, `set-home-activity`
        // refuses), so its whole launcher section skips. An instrumented test enables it from INSIDE
        // the app, which does work — so this is the only place the tablet layout can be exercised on
        // a device at all.
        HomeRoles.enableLauncherComponent(ctx, true);
        // Phone first, as a control: the same code on the same boot must answer 4.
        int phoneCols = readCols();
        assertEquals("the phone grid is not four columns", 4, phoneCols);
        try {
            shell("wm size 2560x1600");
            shell("wm density 240");
            Thread.sleep(2500);
            int sw = ctx.getResources().getConfiguration().smallestScreenWidthDp;
            assertTrue("the resize did not take: smallestScreenWidthDp is still " + sw, sw >= 600);
            int cols = readCols();
            assertTrue("a tablet still draws a phone's grid: " + cols + " columns", cols > 4);
            assertEquals("HomeMetrics and the live configuration disagree",
                    HomeMetrics.deskCols(sw), cols);
            assertTrue("the dock is still a phone's five slots",
                    HomeMetrics.dockMax(ctx.getResources().getConfiguration().screenWidthDp, sw) > 5);
        } finally {
            // UNCONDITIONALLY. A device left resized poisons every test after it on this boot.
            shell("wm size reset");
            shell("wm density reset");
            Thread.sleep(2500);
        }
        assertEquals("the phone grid did not come back", 4, readCols());
    }

    /** Launch the home screen and read the column count it actually laid out with. */
    private int readCols() {
        final int[] out = new int[]{ -1 };
        ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
        try {
            s.onActivity(a -> {
                android.view.ViewGroup host =
                        (android.view.ViewGroup) a.findViewById(place.poster.app.R.id.pc_home_desk);
                DeskView d = (DeskView) host.getChildAt(0);
                out[0] = d.cols();
            });
        } finally {
            s.close();
        }
        return out[0];
    }

    // ------------------------------------------------------------------ tiles added after setup

    /**
     * A TILE THAT BECAME AVAILABLE AFTER THIS PHONE WAS SET UP REACHES THE HOME SCREEN.
     *
     * "posterchan is the default messaging app but still no desktop / app icon ... for text".
     *
     * `seedHome` runs on the FIRST RUN and never again — which is what stops a removed icon coming
     * back, and which means Messages and Phone, withheld from the catalogue until the app held the
     * SMS / dialer role, land nowhere at all on an install that predates that gate being lifted.
     * Driven against the real activity because the thing under test is a decision made during a
     * draw, from stored preferences, on a grid whose size the device decides.
     */
    @Test
    public void aTileThatBecameAvailableLaterReachesTheDesktop() throws Exception {
        HomeRoles.enableLauncherComponent(ctx, true);
        android.content.SharedPreferences sp =
                ctx.getSharedPreferences("pc_home", Context.MODE_PRIVATE);
        java.util.Map<String, ?> before = new java.util.HashMap<String, Object>(sp.getAll());
        try {
            // AN INSTALL THAT ALREADY EXISTED: seeded long ago, with a desktop and a dock that were
            // built while Messages was still gated out, and no record of what had been offered.
            sp.edit().clear()
              .putBoolean("seeded", true)
              .putString("desk", "pc:notes|0|0|1|1")
              .putString("dock", "pc:app")
              .putString("hidden", "")
              .apply();

            java.util.List<String> keys = deskKeysAfterADraw();
            assertTrue("Messages never reached the desktop of an install that predates it: " + keys,
                    keys.contains("pc:" + HomeTiles.VIEW_TEXTS));
            assertTrue("the desktop it already had was thrown away: " + keys,
                    keys.contains("pc:notes"));

            // AND ONLY ONCE. Removing an icon does not hide it, so if the record of what has been
            // offered were not kept, every launch would put it back — the failure this mechanism
            // exists to avoid, and the one that would be reported as "the icon I deleted keeps
            // coming back".
            LauncherPrefs prefs = new LauncherPrefs(ctx);
            java.util.List<Desk.Item> items = Desk.parse(prefs.desk());
            java.util.List<Desk.Item> kept = new java.util.ArrayList<Desk.Item>();
            for (Desk.Item it : items) if (!it.key.equals("pc:" + HomeTiles.VIEW_TEXTS)) kept.add(it);
            assertEquals("nothing was removed, so the second half proves nothing",
                    items.size() - 1, kept.size());
            // WRITTEN TO EVERY GEOMETRY KEY, not just the legacy one. `redrawDesk` reads
            // `desk.<cols>x<rows>`, so removing the icon from the legacy key alone would leave it
            // exactly where it was and this half would prove nothing.
            android.content.SharedPreferences.Editor ed = sp.edit();
            for (String k : sp.getAll().keySet()) if (k.startsWith("desk")) ed.remove(k);
            ed.apply();
            prefs.setDesk(Desk.serialize(kept));
            java.util.List<String> after = deskKeysAfterADraw();
            assertFalse("a tile the person removed was put back: " + after,
                    after.contains("pc:" + HomeTiles.VIEW_TEXTS));
        } finally {
            android.content.SharedPreferences.Editor e = sp.edit().clear();
            for (java.util.Map.Entry<String, ?> row : before.entrySet()) {
                Object v = row.getValue();
                if (v instanceof String) e.putString(row.getKey(), (String) v);
                else if (v instanceof Boolean) e.putBoolean(row.getKey(), (Boolean) v);
                else if (v instanceof Integer) e.putInt(row.getKey(), (Integer) v);
                else if (v instanceof Long) e.putLong(row.getKey(), (Long) v);
            }
            e.apply();
        }
    }

    /** Draw the home screen and read back what ended up on the desktop. */
    private java.util.List<String> deskKeysAfterADraw() throws Exception {
        ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
        try {
            // The adoption runs after the app scan comes back off its own thread.
            Thread.sleep(1500);
        } finally {
            s.close();
        }
        java.util.List<String> out = new java.util.ArrayList<String>();
        for (Desk.Item it : Desk.parse(new LauncherPrefs(ctx).desk())) out.add(it.key);
        return out;
    }

    @Test
    public void theHomeScreenHoldsNoWakeLockWhenItIsNotOnScreen() throws Exception {
        // BATTERY, MEASURED RATHER THAN INTENDED. With the HOME role this process is resident for
        // the life of the battery, so a wake lock taken here is a wake lock held for ever. Read off
        // the real power manager, after the activity has been stopped.
        HomeRoles.enableLauncherComponent(ctx, true);
        ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
        s.moveToState(androidx.lifecycle.Lifecycle.State.CREATED);
        String power = shell("dumpsys power");
        s.close();
        String ownUid = "uid=" + ctx.getApplicationInfo().uid;
        for (String line : power.split("\n")) {
            if (!line.contains("WAKE_LOCK")) continue;
            // WorkSource may list us as a beneficiary of somebody else's lock. Google Play's
            // `Icing` lock does exactly that while indexing an installed package; the owner UID is
            // the first `(uid=...)`, before `ws=WorkSource`. Calling that a launcher-held lock made
            // an otherwise green 68-test device run fail nondeterministically. Our own locks use a
            // posterchan:* tag, and a direct owner UID appears before any WorkSource payload.
            int ws = line.indexOf("ws=WorkSource");
            String owner = ws < 0 ? line : line.substring(0, ws);
            if (line.contains("'posterchan:") || owner.contains("(" + ownUid)) {
                throw new AssertionError("the launcher is holding a wake lock: " + line.trim());
            }
        }
    }

    private static int countWebViews(View v) {
        if (v instanceof WebView) return 1;
        if (!(v instanceof ViewGroup)) return 0;
        ViewGroup g = (ViewGroup) v;
        int n = 0;
        for (int i = 0; i < g.getChildCount(); i++) n += countWebViews(g.getChildAt(i));
        return n;
    }

    private static String shell(String cmd) throws Exception {
        Instrumentation in = InstrumentationRegistry.getInstrumentation();
        try (InputStream is = new android.os.ParcelFileDescriptor.AutoCloseInputStream(
                in.getUiAutomation().executeShellCommand(cmd))) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = is.read(buf)) > 0) out.write(buf, 0, n);
            return out.toString("UTF-8");
        }
    }
}
