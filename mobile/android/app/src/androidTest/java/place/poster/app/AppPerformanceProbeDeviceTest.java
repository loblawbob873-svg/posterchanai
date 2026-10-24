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

import org.junit.Assume;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * MEASURES the app's cost on a phone, the numbers the Android performance work is judged by (2026-10-23).
 *
 * Not a pass/fail gate yet -- it records, under logcat tag PCPERF (one line per number):
 *   cold start   (cold Activity, warm process) launch -> WebView exists -> bundled client ready (__PC) -> first feed paint;
 *   JS heap      after start (Chromium's performance.memory, when the WebView exposes it);
 *   idle CPU     CPU time (clock ticks) every app process -- the main process AND the WebView renderer --
 *                burns over 30 s in the FOREGROUND doing nothing, then 30 s after HOME (backgrounded).
 * Run on the nas emulator: scripts/emulator_tests_on_nas.sh -c AppPerformanceProbeDeviceTest
 */
@RunWith(AndroidJUnit4.class)
public final class AppPerformanceProbeDeviceTest {
    private static final String TAG = "PCPERF";

    @Test public void measureColdStartAndIdleCost() throws Exception {
        /* A MEASUREMENT, NOT A CHECK: it asserts no behaviour and takes minutes, so it runs only when asked for by name
         * (scripts/emulator_tests_on_nas.sh -c AppPerformanceProbeDeviceTest passes `-e class ...`). Run as part of the
         * whole suite it hit the 90 s test timeout and blocked APK publication (CI, 2026-10-24). */
        String asked = String.valueOf(InstrumentationRegistry.getArguments().getString("class"));
        Assume.assumeTrue("the performance probe runs only when its class is named", asked.contains("AppPerformanceProbeDeviceTest"));
        // NOT `am force-stop`: an instrumented test runs IN the app's process, so that killed the probe itself. These
        // are therefore a cold ACTIVITY start (new MainActivity + WebView + page load) in a warm process.
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
            SystemClock.sleep(5000);   // the runner caps a test at 90 s: settle + two 25 s windows + the control
            put("idle_foreground_25s_ticks", ticksOver(25000));
            // What the PAGE does once HOME is pressed: does it learn it is hidden, and what keeps calling back?
            eval(web, "(()=>{const C=window.__bg={raf:0,to:0,iv:0,anim:0,vis:[]};"
                    + "const r=requestAnimationFrame;window.requestAnimationFrame=function(f){return r.call(this,t=>{C.raf++;f(t);});};"
                    + "const st=setTimeout;window.setTimeout=function(f,ms,...a){return st.call(this,typeof f==='function'?function(...b){C.to++;return f.apply(this,b);}:f,ms,...a);};"
                    + "document.addEventListener('visibilitychange',()=>C.vis.push(document.visibilityState));"
                    // every relay message the pool receives, by type (and kind for EVENTs), with bytes
                    + "C.msg={};C.bytes=0;const om=Relay._onMessage;Relay._onMessage=function(c,d){try{C.bytes+=(d&&d.length)||0;"
                    + "const m=JSON.parse(d);const k=m[0]+(m[0]==='EVENT'?':'+((m[2]||{}).kind):'')+' '+((c&&c.url)||'').replace(/^wss?:\\/\\//,'');"
                    + "C.msg[k]=(C.msg[k]||0)+1;}catch(_){}return om.apply(this,arguments);};return true;})()");
            InstrumentationRegistry.getInstrumentation().sendKeyDownUpSync(KeyEvent.KEYCODE_HOME);
            SystemClock.sleep(3000);
            put("idle_background_25s_ticks", ticksOver(25000));
            Log.i(TAG, "background_page " + eval(web, "JSON.stringify({vis:document.visibilityState,hidden:document.hidden,"
                    + "events:__bg.vis,raf:__bg.raf,timeouts:__bg.to,relay_bytes:__bg.bytes,relay_msgs:__bg.msg,"
                    + "animations:(document.getAnimations?document.getAnimations().filter(a=>a.playState==='running').map(a=>(a.animationName||a.constructor.name)+'@'+((a.effect&&a.effect.target&&(a.effect.target.id||a.effect.target.className))||'?')).slice(0,12):'n/a')})"));
            // CONTROL: the same WebView with a blank page, backgrounded the same way -- what the renderer costs with
            // none of our code in it. Our share is the difference.
            final WebView w = web;
            w.post(() -> w.loadUrl("about:blank"));
            SystemClock.sleep(2000);
            put("control_blank_background_25s_ticks", ticksOver(25000));
            Log.i(TAG, "processes=" + RealUi.shell("ps -A -o PID,NAME").replaceAll("(?s)^.*?\\n", "")
                    .lines().filter(l -> l.contains("place.poster") || l.contains("sandboxed_process") || l.contains("webview")).map(String::trim).reduce((a, b) -> a + "; " + b).orElse(""));
            Log.i(TAG, "wakelocks=" + RealUi.shell("dumpsys power").lines()
                    .filter(l -> l.contains("place.poster") && l.contains("WAKE_LOCK")).count());
        } finally {
            scenario.close();
        }
    }

    private static void put(String k, long v) { Log.i(TAG, k + "=" + v); }

    /** utime+stime of every place.poster process (main + renderer + services), summed, over `ms`. */
    private static long ticksOver(long ms) {
        java.util.Map<String, Long> before = perProcess();
        SystemClock.sleep(ms);
        java.util.Map<String, Long> after = perProcess();
        long sum = 0;
        StringBuilder by = new StringBuilder();
        for (java.util.Map.Entry<String, Long> e : after.entrySet()) {
            long d = e.getValue() - before.getOrDefault(e.getKey(), 0L);
            sum += d;
            if (d > 0) by.append(e.getKey()).append('=').append(d).append(' ');
        }
        Log.i(TAG, "by_process " + by.toString().trim());
        return sum;
    }

    /** name(pid) -> utime+stime for every app, WebView renderer and WebView service process. */
    private static java.util.Map<String, Long> perProcess() {
        java.util.Map<String, Long> out = new java.util.TreeMap<>();
        for (String line : RealUi.shell("ps -A -o PID,NAME").split("\n")) {
            String[] f = line.trim().split("\\s+");
            if (f.length < 2 || !(f[1].startsWith("place.poster") || f[1].contains("sandboxed_process") || f[1].contains("webview"))) continue;
            String stat = RealUi.shell("cat /proc/" + f[0] + "/stat").trim();
            int close = stat.lastIndexOf(')');
            if (close < 0) continue;
            String[] st = stat.substring(close + 2).split(" ");
            try { out.put(f[1].replaceAll(".*:", "") + "(" + f[0] + ")", Long.parseLong(st[11]) + Long.parseLong(st[12])); }
            catch (RuntimeException ignored) { }
        }
        return out;
    }

    private static long ticks() {
        long sum = 0;
        for (String line : RealUi.shell("ps -A -o PID,NAME").split("\n")) {
            String[] f = line.trim().split("\\s+");
            // The WebView renderer -- where every line of the client's JS runs -- is a SEPARATE process named after the
            // WebView package (e.g. com.google.android.webview:sandboxed_process0), not after this app.
            if (f.length < 2 || !(f[1].startsWith("place.poster") || f[1].contains("sandboxed_process") || f[1].contains("webview"))) continue;
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
