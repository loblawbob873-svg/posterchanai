package place.poster.app.shortcut;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ResolveInfo;
import android.util.Log;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.ArrayList;
import java.util.List;

/**
 * WHAT AN APP DRAWER SEES — ASKED OF THE PACKAGE MANAGER, WHICH IS THE THING THAT DECIDES.
 *
 * "still see no text app for sms in the app drawer" and "no Email app phone launcher either". Both
 * are one question with one authority: does a MAIN/LAUNCHER query against this package return that
 * component. Nothing else counts — a routing filter (`SENDTO`, `APP_MESSAGING`, `DIAL`) makes an app
 * the phone's default handler and puts it in no drawer at all, which is exactly how Messages and
 * Phone existed for months while their owner correctly reported that they did not.
 *
 * This reads the INSTALLED manifest on a real device, so an alias that is malformed, disabled, or
 * pointing at a target that is not there fails here rather than on somebody's phone.
 */
@RunWith(AndroidJUnit4.class)
public class DrawerAppsDeviceTest {

    private static final String TAG = "PosterChan";

    private Context ctx;
    private String pkg;

    @Before
    public void setUp() {
        ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        pkg = ctx.getPackageName();
    }

    /* THE OPTIONAL ICONS SHIP DISABLED, so every test that is about what the drawer CONTAINS has to
     * say which state it means. They appear when the phone shell is opted into — before that,
     * installing a Nostr client put Texts, Phone, Media Center and Email on somebody's phone, none
     * of them asked for and none reachable ("posterchan apk is showing all the posterchan apps on
     * peoples phones without it being the default launcher").
     *
     * Component state is DEVICE state and survives the test, so it is always put back. */
    private void icons(boolean on) {
        place.poster.app.home.HomeRoles.setAllDrawerIcons(ctx, on);
    }

    @After
    public void tearDown() {
        icons(false);
    }

    private List<ResolveInfo> drawer() {
        Intent i = new Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER)
                .setPackage(pkg);
        return ctx.getPackageManager().queryIntentActivities(i, 0);
    }

    @Test
    public void onlyTheAppItselfIsInTheDrawerUntilTheShellIsEnabled() {
        icons(false);
        List<String> names = new ArrayList<String>();
        for (ResolveInfo r : drawer()) names.add(r.activityInfo.name);
        assertTrue("PosterChan itself is not in the drawer: " + names,
                names.contains("place.poster.app.MainActivity"));
        for (String extra : new String[] {
                "place.poster.app.sms.Messages", "place.poster.app.phone.Phone",
                "place.poster.app.shortcut.MediaCenter", "place.poster.app.shortcut.Email" }) {
            assertTrue(extra + " is in the drawer without anybody opting in: " + names,
                    !names.contains(extra));
        }
    }

    @Test
    public void messagesPhoneAndEmailAreAllInTheDrawer() {
        icons(true);
        List<String> names = new ArrayList<String>();
        List<String> labels = new ArrayList<String>();
        PackageManager pm = ctx.getPackageManager();
        for (ResolveInfo r : drawer()) {
            names.add(r.activityInfo.name);
            labels.add(String.valueOf(r.loadLabel(pm)));
        }
        Log.i(TAG, "drawer probe: " + names + " labelled " + labels);
        assertTrue("PosterChan itself is not in the drawer: " + names,
                names.contains("place.poster.app.MainActivity"));
        assertTrue("no Messages app in the drawer — routing is not an app: " + names,
                names.contains("place.poster.app.sms.Messages"));
        assertTrue("no Phone app in the drawer: " + names,
                names.contains("place.poster.app.phone.Phone"));
        assertTrue("no Email app in the drawer: " + names,
                names.contains("place.poster.app.shortcut.Email"));
    }

    @Test
    public void eachOneLooksLikeItsOwnApp() {
        icons(true);
        // Three drawer entries all showing the PosterChan mark and the PosterChan name is the same
        // complaint as the letter tiles: the app is there and looks like it is not.
        PackageManager pm = ctx.getPackageManager();
        List<String> labels = new ArrayList<String>();
        List<Integer> icons = new ArrayList<Integer>();
        for (ResolveInfo r : drawer()) {
            labels.add(String.valueOf(r.loadLabel(pm)));
            icons.add(r.activityInfo.getIconResource() != 0
                    ? r.activityInfo.getIconResource() : r.activityInfo.applicationInfo.icon);
            assertNotNull("an entry draws no icon at all", r.loadIcon(pm));
        }
        assertEquals("two drawer entries share a name: " + labels,
                labels.size(), new java.util.HashSet<String>(labels).size());
        assertEquals("two drawer entries share an icon: " + labels,
                icons.size(), new java.util.HashSet<Integer>(icons).size());
    }

    @Test
    public void theEmailEntryOpensTheMailViewAndNotJustTheApp() {
        // The whole point of it: "Email" that lands on the timeline is a second PosterChan icon.
        // The view is a manifest fact, so it is read back off the installed manifest — which is also
        // what proves that adding the next one of these needs no Java.
        ComponentName email = new ComponentName(pkg, "place.poster.app.shortcut.Email");
        assertEquals("mail", ViewActivity.viewOf(ctx, email));
    }

    @Test
    public void theTrampolineIsNotAWebViewAndHasNoWindowToLose() {
        // It starts the app and finishes. If it ever became a screen of its own it would be a
        // browser engine sitting between the drawer and the app, which is the one thing the phone
        // shell is built to avoid.
        try {
            android.content.pm.ActivityInfo ai = ctx.getPackageManager().getActivityInfo(
                    new ComponentName(pkg, ViewActivity.class.getName()), 0);
            assertNotNull(ai);
            assertTrue("the trampoline is exported — it is an internal target, not a door",
                    !ai.exported);
        } catch (PackageManager.NameNotFoundException e) {
            throw new AssertionError("the Email alias points at an activity that is not installed", e);
        }
    }
}
