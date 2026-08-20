package place.poster.app.home;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.app.Instrumentation;
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
                new java.util.ArrayList<AppShelf.Entry>(), HomeTiles.ours(false, false),
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
    public void theHomeScreenHoldsNoWakeLockWhenItIsNotOnScreen() throws Exception {
        // BATTERY, MEASURED RATHER THAN INTENDED. With the HOME role this process is resident for
        // the life of the battery, so a wake lock taken here is a wake lock held for ever. Read off
        // the real power manager, after the activity has been stopped.
        HomeRoles.enableLauncherComponent(ctx, true);
        ActivityScenario<HomeActivity> s = ActivityScenario.launch(HomeActivity.class);
        s.moveToState(androidx.lifecycle.Lifecycle.State.CREATED);
        String power = shell("dumpsys power");
        s.close();
        for (String line : power.split("\n")) {
            if (line.contains("WAKE_LOCK") && line.contains(ctx.getPackageName())) {
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
