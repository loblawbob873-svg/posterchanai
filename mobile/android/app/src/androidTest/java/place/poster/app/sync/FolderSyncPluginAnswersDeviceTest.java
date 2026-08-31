package place.poster.app.sync;

import static org.junit.Assert.assertTrue;

import android.os.SystemClock;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;

import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

import place.poster.app.MainActivity;

/**
 * THE PLUGIN CALL FOLDER SYNC MAKES AS IT PAINTS MUST ANSWER, AND MUST NOT TAKE THE APP WITH IT.
 *
 * "Folder Sync just crashes the app and returns you to desktop." "If you launch it a few times,
 * android says: there is a bug and it closed." "Folder sync still not even opening on android! then
 * I get the prompt to clear the cache for PosterChan!"
 *
 * Opening the screen runs {@code _readNativeLast} (sync.js), which calls {@code nativeReport}. That
 * method hands a Runnable to {@code getBridge().execute}, and a bridge task runs on a handler
 * thread AFTER Capacitor's own try/catch has returned — so a throw inside it has no catch above it
 * and Android's default handler ends the PROCESS. The app is gone before the screen can draw, which
 * is exactly the shape of every report above.
 *
 * The other half is quieter and is asserted here too: a guard that swallows without answering
 * leaves the JavaScript promise pending for ever, and on this screen that is a spinner that never
 * resolves. A liveness check alone cannot tell that from success.
 *
 * Deliberately NOT routed through the view. {@code 'sync'} is instance-gated and this bundle has no
 * instance, so reaching the screen means reproducing the client's gating from a test — internals it
 * does not export, which is how a test ends up asserting against its own scaffolding. The plugin
 * call is the thing that crashes; it is called directly.
 */
@RunWith(AndroidJUnit4.class)
public final class FolderSyncPluginAnswersDeviceTest {

    @Test public void everyFolderSyncPluginReadAnswersAndLeavesTheProcessAlive() throws Exception {
        ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class);
        try {
            AtomicReference<WebView> ref = new AtomicReference<WebView>();
            WebView web = waitForWebView(scenario, ref);

            String ready = "";
            for (int i = 0; i < 150; i++) {
                ready = eval(web, "document.readyState+'|'+!!(window.Capacitor&&Capacitor.Plugins)");
                if (ready.contains("complete|true")) break;
                SystemClock.sleep(100);
            }
            assertTrue("the bridge never came up: " + ready, ready.contains("complete|true"));

            String have = eval(web, "!!(Capacitor.Plugins&&Capacitor.Plugins.FolderSync)");
            assertTrue("the FolderSync plugin is not registered in the shipped APK", have.contains("true"));

            /* The reads the Folder Sync screen makes while painting. Every one of them is a bridge
             * task, so every one of them is a way to end the process. */
            String[] methods = {"nativeReport", "nativeLive", "power"};
            for (String m : methods) {
                String asked = eval(web,
                        "(()=>{const P=Capacitor.Plugins.FolderSync;"
                        + "if(typeof P['" + m + "']!=='function')return 'absent';"
                        + "window.__pcAns='pending';"
                        + "try{P['" + m + "']().then(()=>{window.__pcAns='resolved';},"
                        + "e=>{window.__pcAns='rejected:'+String(e&&e.message||e);});}"
                        + "catch(e){window.__pcAns='threw:'+String(e);}"
                        + "return 'asked';})()");
                if (asked.contains("absent")) continue;

                String settled = "pending";
                for (int i = 0; i < 100; i++) {          // ten seconds; these are prefs reads
                    settled = eval(web, "window.__pcAns");
                    if (!settled.contains("pending")) break;
                    SystemClock.sleep(100);
                }
                assertTrue(m + " never answered — the screen that calls it would spin for ever",
                        !settled.contains("pending"));
                assertTrue(m + " threw synchronously: " + settled, !settled.startsWith("threw:"));

                /* A rejection is a fine answer; a dead process is not. If the bridge task threw, the
                 * app is already gone and this is where the run turns red, with the stack trace in
                 * the logcat artifact rather than in a user's evening. */
                scenario.onActivity(a -> assertTrue("MainActivity is gone after calling " + m,
                        !a.isFinishing() && !a.isDestroyed()));
                AtomicReference<WebView> now = new AtomicReference<WebView>();
                scenario.onActivity(a -> now.set(findWebView(a.findViewById(android.R.id.content))));
                assertTrue("the renderer was replaced by " + m, now.get() == web);
            }

            /* Called repeatedly, because the report is "if you launch it a few times". A fault that
             * needs a second pass through the same code is still a fault. */
            for (int round = 0; round < 5; round++) {
                eval(web, "(()=>{try{Capacitor.Plugins.FolderSync.nativeReport();}catch(e){}return 1;})()");
                SystemClock.sleep(200);
            }
            SystemClock.sleep(1500);
            scenario.onActivity(a -> assertTrue("the app died after repeated Folder Sync reads",
                    !a.isFinishing() && !a.isDestroyed()));
            assertTrue("the renderer stopped answering after repeated Folder Sync reads",
                    eval(web, "1+1").contains("2"));
        } finally {
            scenario.close();
        }
    }

    private static WebView waitForWebView(ActivityScenario<MainActivity> scenario,
                                          AtomicReference<WebView> ref) {
        for (int i = 0; i < 150; i++) {
            scenario.onActivity(a -> ref.set(findWebView(a.findViewById(android.R.id.content))));
            if (ref.get() != null) return ref.get();
            SystemClock.sleep(100);
        }
        throw new AssertionError("no WebView ever appeared");
    }

    private static WebView findWebView(View v) {
        if (v instanceof WebView) return (WebView) v;
        if (v instanceof ViewGroup) {
            ViewGroup g = (ViewGroup) v;
            for (int i = 0; i < g.getChildCount(); i++) {
                WebView w = findWebView(g.getChildAt(i));
                if (w != null) return w;
            }
        }
        return null;
    }

    private static String eval(WebView web, String js) throws Exception {
        final CountDownLatch done = new CountDownLatch(1);
        final AtomicReference<String> out = new AtomicReference<String>("");
        web.post(() -> web.evaluateJavascript(js, value -> { out.set(value == null ? "" : value); done.countDown(); }));
        if (!done.await(30, TimeUnit.SECONDS)) throw new AssertionError("evaluateJavascript timed out: " + js);
        String s = out.get();
        if (s.length() > 1 && s.charAt(0) == '"' && s.charAt(s.length() - 1) == '"') {
            s = s.substring(1, s.length() - 1).replace("\\\"", "\"").replace("\\\\", "\\").replace("\\n", "\n");
        }
        return s;
    }
}
