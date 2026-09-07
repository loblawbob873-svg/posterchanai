package place.poster.app.home;

import static org.junit.Assert.*;

import android.app.Instrumentation;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.SystemClock;
import android.accessibilityservice.AccessibilityService;
import android.view.accessibility.AccessibilityNodeInfo;
import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.junit.Test;
import org.junit.runner.RunWith;
import place.poster.app.R;

/** Exercise the actual long-press menu and Android's confirmation, without uninstalling anything. */
@RunWith(AndroidJUnit4.class)
public class UninstallDeviceTest {
    private Instrumentation instrumentation() { return InstrumentationRegistry.getInstrumentation(); }
    private Context ctx() { return instrumentation().getTargetContext(); }

    @Test public void installedLauncherHasPermissionToRequestUninstall() {
        assertEquals(PackageManager.PERMISSION_GRANTED, ctx().getPackageManager().checkPermission(
                "android.permission.REQUEST_DELETE_PACKAGES", ctx().getPackageName()));
    }

    @Test public void builtInTilesCannotRequestUninstall() {
        AppRepo repo = new AppRepo(ctx());
        assertFalse(repo.uninstall(null));
        assertFalse(repo.uninstall(AppShelf.Entry.ours("phone", "Phone", true)));
    }

    @Test public void drawerUninstallOpensSystemConfirmationAndCancelKeepsPackage() throws Exception {
        Instrumentation in = instrumentation();
        String fixture = in.getContext().getPackageName(); // the installed test APK, never user data
        boolean enabled = HomeRoles.launcherComponentEnabled(ctx());
        HomeRoles.enableLauncherComponent(ctx(), true);
        try (ActivityScenario<HomeActivity> scenario = ActivityScenario.launch(new Intent(ctx(), HomeActivity.class))) {
            scenario.onActivity(home -> {
                try {
                    java.lang.reflect.Method menu = HomeActivity.class.getDeclaredMethod("drawerMenu", AppShelf.Entry.class);
                    menu.setAccessible(true);
                    menu.invoke(home, AppShelf.Entry.app(fixture, "", "Uninstall test fixture"));
                } catch (Exception e) { throw new AssertionError(e); }
            });
            in.waitForIdleSync();
            AccessibilityNodeInfo root = in.getUiAutomation().getRootInActiveWindow();
            assertNotNull(root);
            assertEquals("Only click Uninstall in our menu, never in the system confirmation",
                    ctx().getPackageName(), String.valueOf(root.getPackageName()));
            boolean clicked = false;
            for (AccessibilityNodeInfo node : root.findAccessibilityNodeInfosByText(ctx().getString(R.string.home_uninstall))) {
                if (ctx().getString(R.string.home_uninstall).contentEquals(node.getText() == null ? "" : node.getText())) {
                    clicked = node.performAction(AccessibilityNodeInfo.ACTION_CLICK);
                    if (clicked) break;
                }
            }
            assertTrue("Uninstall menu item was not clickable", clicked);
            boolean confirmation = false;
            long deadline = SystemClock.uptimeMillis() + 8000;
            while (SystemClock.uptimeMillis() < deadline) {
                root = in.getUiAutomation().getRootInActiveWindow();
                String pkg = root == null ? "" : String.valueOf(root.getPackageName());
                if (pkg.contains("packageinstaller") || pkg.contains("permissioncontroller")) {
                    confirmation = true;
                    break;
                }
                SystemClock.sleep(100);
            }
            assertTrue("Android never opened its uninstall confirmation", confirmation);
            String fixtureLabel = ctx().getPackageManager().getApplicationLabel(
                    ctx().getPackageManager().getApplicationInfo(fixture, 0)).toString();
            assertFalse("Confirmation does not identify the fixture", root.findAccessibilityNodeInfosByText(fixtureLabel).isEmpty());
            assertTrue(in.getUiAutomation().performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK)); // NEVER confirm removal
            long cancelDeadline = SystemClock.uptimeMillis() + 5000;
            boolean dismissed = false;
            while (SystemClock.uptimeMillis() < cancelDeadline) {
                root = in.getUiAutomation().getRootInActiveWindow();
                String pkg = root == null ? "" : String.valueOf(root.getPackageName());
                if (ctx().getPackageName().equals(pkg)) { dismissed = true; break; }
                SystemClock.sleep(100);
            }
            assertTrue("Cancel did not close the system confirmation", dismissed);
            assertNotNull(ctx().getPackageManager().getPackageInfo(fixture, 0));
        } finally {
            try {
                AccessibilityNodeInfo root = in.getUiAutomation().getRootInActiveWindow();
                String pkg = root == null ? "" : String.valueOf(root.getPackageName());
                if (pkg.contains("packageinstaller") || pkg.contains("permissioncontroller"))
                    in.getUiAutomation().performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK);
            } finally { HomeRoles.enableLauncherComponent(ctx(), enabled); }
        }
    }
}
