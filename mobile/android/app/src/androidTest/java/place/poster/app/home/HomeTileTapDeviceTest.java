package place.poster.app.home;

import android.content.Context;
import android.content.Intent;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

import place.poster.app.RealUi;

/**
 * THE HOME SCREEN'S TILES, PRESSED WITH A FINGER.
 *
 * LauncherDeviceTest opens Texts by calling HomeActivity's private `open()` through reflection, and
 * LauncherTileLandsOnItsScreenDeviceTest fires the tile's INTENT. Neither proves a person can press the
 * tile: that it is drawn where Android thinks it is, inside the window, not covered by the dock, a
 * wallpaper offer or a hint, and that a real touch on it (DeskView's own gesture handling, which also
 * decides long-press-to-edit and swipe-up-for-the-drawer) is read as a TAP. Here every press is a real
 * touch at the centre of the label a person reads, and what opens is checked by what is ON SCREEN.
 */
@RunWith(AndroidJUnit4.class)
public class HomeTileTapDeviceTest {

    private Context ctx;
    private boolean wasEnabled;

    @Before
    public void setUp() {
        ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        wasEnabled = HomeRoles.launcherComponentEnabled(ctx);
        HomeRoles.enableLauncherComponent(ctx, true);
        RealUi.awake();
    }

    @After
    public void tearDown() {
        RealUi.shell("input keyevent KEYCODE_HOME");
        HomeRoles.enableLauncherComponent(ctx, wasEnabled);
    }

    @Test
    public void tappingThePhoneTileOpensTheKeypad() {
        home();
        RealUi.tap("the Phone tile", RealUi.named("Phone"), 10_000);
        RealUi.waitFor("the Phone app's keypad, after tapping the Phone tile", RealUi.id("pc_dl_pad"), 10_000);
    }

    @Test
    public void tappingTheTextsTileOpensTheConversationList() {
        home();
        RealUi.tap("the Texts tile", RealUi.named("Texts"), 10_000);
        RealUi.waitFor("the Messages list, after tapping the Texts tile", RealUi.id("pc_sms_title"), 10_000);
    }

    /** Out to an app and back to the home screen, and the tiles still answer a tap: a home screen that
     *  only works on its first appearance is one that "stops responding" after the first app you open. */
    @Test
    public void afterComingBackTheTilesStillAnswer() {
        home();
        RealUi.tap("the Phone tile", RealUi.named("Phone"), 10_000);
        RealUi.waitFor("the Phone app's keypad", RealUi.id("pc_dl_pad"), 10_000);
        home();
        RealUi.tap("the Texts tile, after coming back", RealUi.named("Texts"), 10_000);
        RealUi.waitFor("the Messages list, after coming back to the home screen", RealUi.id("pc_sms_title"), 10_000);
    }

    private void home() {
        ctx.startActivity(new Intent(ctx, HomeActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP));
        // The home screen is drawn when its desk is, and only once the app it covered is gone (the
        // Phone app's own title says "Phone" too); a tap before that lands on something else.
        try {
            RealUi.waitUntil("PosterChan's home screen never came up with its Phone tile", 15_000,
                    () -> RealUi.find(RealUi.id("pc_home_desk")) != null
                            && RealUi.find(RealUi.id("pc_dl_pad")) == null
                            && RealUi.find(RealUi.id("pc_sms_title")) == null
                            && RealUi.find(RealUi.named("Phone")) != null);
        } catch (Exception e) {
            throw new AssertionError(e);
        }
    }
}
