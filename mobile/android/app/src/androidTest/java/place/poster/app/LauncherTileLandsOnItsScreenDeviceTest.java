package place.poster.app;

import static org.junit.Assert.assertTrue;

import android.content.Intent;
import android.os.SystemClock;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicReference;

import place.poster.app.home.HomeActivity;
import place.poster.app.home.HomeTiles;
import place.poster.app.home.LaunchView;

/**
 * EVERY LAUNCHER TILE, THROUGH THE REAL INTENT PATH, LANDS ON ITS OWN SCREEN.
 *
 *     "when I click on Messages from the android launcher, it does not load messages for me"
 *
 * The coverage that existed sat on both sides of this seam and never crossed it.
 * `AppViewsLaunchSmokeTest` opens every view by calling `__PC.switchView(view)` in JS — it proves
 * the screens work and never touches an intent. `test_android_launch_view.py` checks that every
 * tile NAMES a view the client has — a tile can name a valid view and still land nowhere.
 * `LauncherDeviceTest` walks one tile, and that tile is Texts, a native Activity.
 *
 * This walks tile → parked request → intent → MainActivity → HomePlugin → the client's own
 * `switchView` → a painted screen, for EVERY tile the shipped catalogue offers. The list is
 * `HomeTiles.catalogue()`, never a list typed into this file, so a tile added later joins the gate
 * on its first build.
 *
 * WHAT IS REAL AND WHAT IS NOT. Each tile is delivered by `startActivity` with the intent
 * `HomeActivity.openApp` builds — a genuine ActivityManager delivery into the already-running
 * singleTask activity, i.e. the warm press, which is the half where the extras used to be dropped.
 * What is not paid is a fresh PROCESS per tile: 40 cold starts is minutes of emulator time and
 * would make this the slowest gate in the suite. ONE tile — Messages, the reported one —
 * additionally goes through a full `ActivityScenario.launch(intent)` cold start, carrying a stamp
 * older than the staleness window, which is exactly the shape a tapped notification has.
 */
@RunWith(AndroidJUnit4.class)
public final class LauncherTileLandsOnItsScreenDeviceTest {

    /** Tiles that deliberately open something other than a client view. */
    private static boolean isAView(String view) {
        if (HomeTiles.VIEW_APP.equals(view)) return false;              // "open the app, no screen"
        if (HomeTiles.VIEW_SETTINGS.equals(view)) return false;         // the phone's own Settings
        return HomeTiles.nativeTarget(view).isEmpty();                  // Phone / Texts are Activities
    }

    @Test public void everyTileOpensTheScreenItNames() throws Exception {
        ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class);
        try {
            WebView web = ready(scenario);
            List<String> failures = new ArrayList<String>();
            int covered = 0;
            for (HomeTiles.Tile tile : HomeTiles.catalogue()) {
                if (!isAView(tile.view)) continue;
                covered++;
                // Exactly what HomeActivity.openApp does, in the same order: park BEFORE the start,
                // because on a fast device the target can resume and read before the next line.
                LaunchView.request(tile.view, System.currentTimeMillis());
                final Intent i = new Intent(InstrumentationRegistry.getInstrumentation()
                        .getTargetContext(), MainActivity.class)
                        .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                        .putExtra(HomeActivity.EXTRA_VIEW, tile.view)
                        .putExtra(HomeActivity.EXTRA_VIEW_AT, System.currentTimeMillis());
                // A REAL delivery: startActivity at a singleTask activity that is already
                // running is what the ActivityManager turns into onNewIntent.
                scenario.onActivity(a -> a.startActivity(i));

                String state = settle(web, tile.view);
                if (!state.startsWith("ok")) failures.add(tile.view + ": " + state);
            }
            // A catalogue that came back empty would make every assertion above vacuous.
            assertTrue("suspiciously few tiles covered: " + covered, covered >= 30);
            assertTrue("launcher tiles that did not open their own screen: " + failures,
                       failures.isEmpty());
        } finally {
            scenario.close();
        }
    }

    /**
     * THE COLD START, for the one tile the report named.
     *
     * A warm press is announced straight to the page by onNewIntent; a cold one has only the parked
     * request and the intent extra, and that is the half where a notification's route used to be
     * thrown away as stale.
     */
    @Test public void aColdLaunchAlsoLandsOnMessages() throws Exception {
        LaunchView.clear();
        Intent i = new Intent(InstrumentationRegistry.getInstrumentation().getTargetContext(),
                              MainActivity.class)
                .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP)
                .putExtra(HomeActivity.EXTRA_VIEW, "messages")
                // A NOTIFICATION'S STAMP IS ITS POSTING TIME, and one left in the shade for a few
                // minutes used to lose its whole deep link to the staleness rule. Reproduce that
                // exactly: an extra far older than LaunchView.MAX_AGE_MS, delivered fresh.
                .putExtra(HomeActivity.EXTRA_VIEW_AT,
                          System.currentTimeMillis() - (LaunchView.MAX_AGE_MS * 5));
        ActivityScenario<MainActivity> scenario = ActivityScenario.launch(i);
        try {
            WebView web = ready(scenario);
            String state = settle(web, "messages");
            assertTrue("a cold launch did not land on Messages: " + state, state.startsWith("ok"));
        } finally {
            scenario.close();
        }
    }

    /** Wait for the client to adopt `view` and paint something, or describe what it did instead.
     *
     * IT ANSWERS A BARE TOKEN, NOT JSON, and that is the whole reason this works.
     * `evaluateJavascript` hands back a JSON-ENCODED value, so a JS string arrives at Java already
     * escaped: `{"active":true}` comes through as `"{\"active\":true}"`. The first version matched
     * on `"active":true`, which is never present in that text — so this NEVER returned ok, every
     * probe burned its full budget, `aColdLaunchAlsoLandsOnMessages` failed while reporting a state
     * that reads as success, and the per-tile walk spent six seconds a tile and blew the runner's
     * 90-second per-test timeout. One escaping mistake, two red tests, and a failure message that
     * argued against itself.
     *
     * The budget is also smaller now: this runs once per tile across the whole shipped catalogue,
     * so a probe that cannot settle must cost ~2s, not 6.
     */
    private static String settle(WebView web, String view) {
        String last = "";
        for (int i = 0; i < 20; i++) {          // the landing runs after boot, then paints
            SystemClock.sleep(100);
            last = eval(web, "(()=>{const f=document.getElementById('feed');"
                    + "const a=__PC.isView(" + JSONObject.quote(view) + "),"
                    + "p=!!(f&&f.children.length);"
                    + "return a&&p?'ok':('active='+a+' painted='+p);})()");
            // The value is JSON-encoded, so an ok answer arrives as the four characters "ok".
            if (last.contains("ok")) return "ok";
        }
        return last;
    }

    private static WebView ready(ActivityScenario<MainActivity> scenario) throws Exception {
        AtomicReference<WebView> ref = new AtomicReference<WebView>();
        WebView web = null;
        for (int i = 0; i < 80 && web == null; i++) {
            scenario.onActivity(a -> ref.set(find(a.findViewById(android.R.id.content))));
            web = ref.get();
            if (web == null) SystemClock.sleep(100);
        }
        if (web == null) throw new AssertionError("MainActivity never created its WebView");
        String state = "";
        for (int i = 0; i < 150; i++) {
            state = eval(web, "document.readyState+'|'+!!window.__PC+'|'+!!document.getElementById('feed')");
            if (state.contains("complete|true|true")) return web;
            SystemClock.sleep(100);
        }
        throw new AssertionError("bundled client never became ready: " + state);
    }

    private static WebView find(View view) {
        if (view instanceof WebView) return (WebView) view;
        if (view instanceof ViewGroup) {
            ViewGroup g = (ViewGroup) view;
            for (int i = 0; i < g.getChildCount(); i++) {
                WebView w = find(g.getChildAt(i));
                if (w != null) return w;
            }
        }
        return null;
    }

    private static String eval(WebView web, String js) {
        final AtomicReference<String> out = new AtomicReference<String>("");
        final java.util.concurrent.CountDownLatch done = new java.util.concurrent.CountDownLatch(1);
        web.post(() -> web.evaluateJavascript(js, value -> { out.set(String.valueOf(value)); done.countDown(); }));
        try { done.await(10, java.util.concurrent.TimeUnit.SECONDS); } catch (InterruptedException ignored) { }
        return out.get();
    }
}
