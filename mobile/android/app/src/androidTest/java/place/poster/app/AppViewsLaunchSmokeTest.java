package place.poster.app;

import static org.junit.Assert.assertTrue;

import android.os.SystemClock;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;

import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * Opens every user-facing client view in the APK's real Chromium renderer.
 *
 * The list deliberately comes from the shipped navigation rather than being copied into the test.
 * A new app therefore joins this gate as soon as its navigation row ships.  This is a launch smoke
 * gate, not a screenshot test: every route must paint a non-empty view, leave the Activity and its
 * WebView/document alive, and raise no uncaught JavaScript error or unhandled rejection.
 */
@RunWith(AndroidJUnit4.class)
public final class AppViewsLaunchSmokeTest {
    @Test public void everyShippedViewOpensWithoutKillingOrReplacingTheApp() throws Exception {
        ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class);
        try {
            AtomicReference<WebView> ref = new AtomicReference<WebView>();
            WebView web = waitForWebView(scenario, ref);

            String ready = "";
            for (int i = 0; i < 150; i++) {
                ready = eval(web, "document.readyState+'|'+!!window.__PC+'|'+!!document.getElementById('feed')");
                if (ready.contains("complete|true|true")) break;
                SystemClock.sleep(100);
            }
            assertTrue("bundled client never became ready: " + ready,
                    ready.contains("complete|true|true"));

            // Install the probes before the first route. Resource-load failures are intentionally
            // excluded: window.onerror only receives them in capture mode and they are not uncaught
            // JavaScript. Runtime exceptions and rejected promises are the renderer-crash precursors
            // this gate exists to catch.
            eval(web, "(()=>{window.__pcLaunchErrors=[];window.__pcLaunchDocument={};"
                    + "document.__pcLaunchDocument=window.__pcLaunchDocument;"
                    + "addEventListener('error',e=>{if(e&&e.error)__pcLaunchErrors.push(String(e.error.stack||e.error));});"
                    + "addEventListener('unhandledrejection',e=>__pcLaunchErrors.push(String((e.reason&&e.reason.stack)||e.reason)));"
                    + "return true;})()");

            JSONArray names = new JSONArray(eval(web,
                    "(()=>{const seen=new Set();return [...document.querySelectorAll('.sidebar .nav .nav-item[data-view]')]"
                    + ".map(x=>x.dataset.view).filter(x=>x&&!seen.has(x)&&seen.add(x));})()"));
            // This catches a stale/empty bundled shell turning a dynamic test into a green no-op.
            assertTrue("shipped app registry is unexpectedly small: " + names, names.length() >= 35);

            List<String> failures = new ArrayList<String>();
            for (int i = 0; i < names.length(); i++) {
                String view = names.getString(i);
                eval(web, "(()=>{window.__pcLaunchErrors.length=0;try{__PC.switchView("
                        + JSONObject.quote(view) + ");return 'started';}catch(e){return 'throw:'+String(e.stack||e);}})()");
                // Module-backed views load their script asynchronously.  Give that path a real turn
                // rather than only proving switchView returned before it painted anything.
                SystemClock.sleep(500);

                AtomicReference<WebView> current = new AtomicReference<WebView>();
                scenario.onActivity(a -> {
                    assertTrue("MainActivity is finishing after opening " + view,
                            !a.isFinishing() && !a.isDestroyed());
                    current.set(findWebView(a.findViewById(android.R.id.content)));
                });
                if (current.get() != web) {
                    failures.add(view + ": WebView was replaced");
                    break;
                }

                JSONObject state = new JSONObject(eval(web,
                        "(()=>({active:__PC.isView(" + JSONObject.quote(view) + "),"
                        + "painted:!!(document.getElementById('feed')&&document.getElementById('feed').children.length),"
                        + "sameDocument:document.__pcLaunchDocument===window.__pcLaunchDocument,"
                        + "errors:window.__pcLaunchErrors.slice()}))()"));
                if (!state.getBoolean("active")) failures.add(view + ": route was replaced by another view");
                if (!state.getBoolean("painted")) failures.add(view + ": rendered an empty feed");
                if (!state.getBoolean("sameDocument")) failures.add(view + ": document was replaced");
                JSONArray errors = state.getJSONArray("errors");
                if (errors.length() != 0) failures.add(view + ": uncaught JS " + errors);
            }
            assertTrue("app launch smoke failures: " + failures, failures.isEmpty());
        } finally {
            scenario.close();
        }
    }

    private static WebView waitForWebView(ActivityScenario<MainActivity> scenario,
                                          AtomicReference<WebView> ref) throws Exception {
        for (int i = 0; i < 80; i++) {
            scenario.onActivity(a -> ref.set(findWebView(a.findViewById(android.R.id.content))));
            if (ref.get() != null) return ref.get();
            SystemClock.sleep(100);
        }
        throw new AssertionError("MainActivity never created its WebView");
    }

    private static WebView findWebView(View view) {
        if (view instanceof WebView) return (WebView) view;
        if (!(view instanceof ViewGroup)) return null;
        ViewGroup group = (ViewGroup) view;
        for (int i = 0; i < group.getChildCount(); i++) {
            WebView found = findWebView(group.getChildAt(i));
            if (found != null) return found;
        }
        return null;
    }

    private static String eval(WebView web, String js) throws Exception {
        CountDownLatch done = new CountDownLatch(1);
        AtomicReference<String> result = new AtomicReference<String>("null");
        web.post(() -> web.evaluateJavascript(js, value -> {
            result.set(value);
            done.countDown();
        }));
        assertTrue("WebView renderer stopped answering JavaScript", done.await(15, TimeUnit.SECONDS));
        return result.get();
    }
}
