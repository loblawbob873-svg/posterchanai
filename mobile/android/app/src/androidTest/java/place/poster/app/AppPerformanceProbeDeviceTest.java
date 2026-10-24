package place.poster.app;

import static org.junit.Assert.assertTrue;

import android.os.SystemClock;
import android.util.Log;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * MEASURES the app's cost on a phone, the numbers the Android performance work is judged by (2026-10-23).
 *
 * Not a pass/fail gate yet -- it records, under logcat tag PCPERF (one line per number):
 *   cold start   launch -> WebView exists -> bundled client ready (__PC) -> first feed paint;
 *   JS heap      after start (Chromium's performance.memory, when the WebView exposes it);
 *   idle CPU     CPU time (clock ticks) every app process -- the main process AND the WebView renderer --
 *                burns over 60 s in the FOREGROUND doing nothing, then 60 s after HOME (backgrounded).
 * Run on the nas emulator: scripts/emulator_tests_on_nas.sh -c AppPerformanceProbeDeviceTest
 */
@RunWith(AndroidJUnit4.class)
public final class AppPerformanceProbeDeviceTest {
    private static final String TAG = "PCPERF";

    @Test public void measureColdStartAndIdleCost() throws Exception {
        RealUi.shell("am force-stop place.poster.app");
        SystemClock.sleep(1500);
        long t0 = SystemClock.elapsedRealtime();
        ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class);
        try {
            AtomicReference<WebView> ref = new AtomicReference<WebView>();
            WebView web = null;
            for (int i = 0; i < 300 && web == null; i++) {
                scenario.onActivity(a -> ref.set(find(a.findViewById(android.R.id.content))));
                web = ref.get();
                if (web == null) SystemClock.sleep(20);
            }
            assertTrue("no WebView", web != null);
            long tWeb = SystemClock.elapsedRealtime();
            long tReady = -1, tPaint = -1;
            for (int i = 0; i < 600; i++) {
                String s = eval(web, "(!!window.__PC)+'|'+!!(document.getElementById('feed')&&document.getElementById('feed').children.length)");
                long now = SystemClock.elapsedRealtime();
                if (tReady < 0 && s.contains("true|")) tReady = now;
                if (s.contains("|true")) { tPaint = now; break; }
                SystemClock.sleep(50);
            }
            put("cold_start_webview_ms", tWeb - t0);
            put("cold_start_client_ready_ms", tReady < 0 ? -1 : tReady - t0);
            put("cold_start_first_paint_ms", tPaint < 0 ? -1 : tPaint - t0);
            String mem = eval(web, "(performance.memory?Math.round(performance.memory.usedJSHeapSize/1048576)+'/'"
                    + "+Math.round(performance.memory.totalJSHeapSize/1048576):'n/a')");
            Log.i(TAG, "js_heap_used_total_mb=" + mem.replace("\"", ""));
            // Settle (startup work, relay connects), then the idle windows.
            SystemClock.sleep(20000);
            put("idle_foreground_60s_ticks", ticksOver(60000));
            InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_HOME);
            SystemClock.sleep(5000);
            put("idle_background_60s_ticks", ticksOver(60000));
            Log.i(TAG, "processes=" + RealUi.shell("ps -A -o PID,NAME").replaceAll("(?s)^.*?\\n", "")
                    .lines().filter(l -> l.contains("place.poster")).map(String::trim).reduce((a, b) -> a + "; " + b).orElse(""));
            Log.i(TAG, "wakelocks=" + RealUi.shell("dumpsys power").lines()
                    .filter(l -> l.contains("place.poster") && l.contains("WAKE_LOCK")).count());
        } finally {
            scenario.close();
        }
    }

    private static void put(String k, long v) { Log.i(TAG, k + "=" + v); }

    /** utime+stime of every place.poster process (main + renderer + services), summed, over `ms`. */
    private static long ticksOver(long ms) {
        long a = ticks();
        SystemClock.sleep(ms);
        return ticks() - a;
    }

    private static long ticks() {
        long sum = 0;
        for (String line : RealUi.shell("ps -A -o PID,NAME").split("\n")) {
            String[] f = line.trim().split("\\s+");
            if (f.length < 2 || !f[1].startsWith("place.poster")) continue;
            String stat = RealUi.shell("cat /proc/" + f[0] + "/stat").trim();
            int close = stat.lastIndexOf(')');
            if (close < 0) continue;
            String[] s = stat.substring(close + 2).split(" ");
            try { sum += Long.parseLong(s[11]) + Long.parseLong(s[12]); } catch (RuntimeException ignored) { }
        }
        return sum;
    }

    private static WebView find(View view) {
        if (view instanceof WebView) return (WebView) view;
        if (!(view instanceof ViewGroup)) return null;
        ViewGroup g = (ViewGroup) view;
        for (int i = 0; i < g.getChildCount(); i++) { WebView w = find(g.getChildAt(i)); if (w != null) return w; }
        return null;
    }

    private static String eval(WebView web, String js) throws Exception {
        CountDownLatch done = new CountDownLatch(1);
        AtomicReference<String> result = new AtomicReference<String>("null");
        web.post(() -> web.evaluateJavascript(js, v -> { result.set(v); done.countDown(); }));
        assertTrue("WebView renderer stopped answering JavaScript", done.await(15, TimeUnit.SECONDS));
        return result.get();
    }
}
