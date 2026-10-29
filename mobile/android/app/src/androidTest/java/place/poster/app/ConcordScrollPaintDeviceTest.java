package place.poster.app;

import static org.junit.Assert.assertTrue;

import android.app.UiAutomation;
import android.graphics.Bitmap;
import android.os.SystemClock;
import android.util.Log;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.json.JSONArray;
import org.junit.Assume;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;

/**
 * "Smoother Community Scrolling": flinging a Concord room showed BLANK areas for a second before the
 * messages painted. JavaScript cannot see that -- every row is in the DOM the whole time; what is missing is
 * the RASTER -- so this measures what is on the SCREEN: a real fling (injected touchscreen events, the
 * path a finger takes), then screenshots for the length of the fling, counting the pixel rows of the
 * message pane that are blank (one flat colour, in a band taller than any gap a painted room has).
 * One line per frame under logcat tag PCSCROLL.
 */
@RunWith(AndroidJUnit4.class)
public final class ConcordScrollPaintDeviceTest {
    private static final String TAG = "PCSCROLL";

    /** {name, css, js run once as the fling starts, js run every 150 ms during it} */
    @Test public void textRoomOnTheCiScreen() throws Exception {
        room(false, new String[][] {
            {"shipped", "", "", ""},
            // POSITIVE CONTROL: every third row unpainted. A metric that reads 0% here is blind.
            {"control-hidden-rows", ".cc-message:nth-child(3n){visibility:hidden!important}", "", ""},
        }, false);
    }

    @Test public void textRoomAtPhoneDensity() throws Exception {
        room(true, new String[][] {
            {"phone-shipped", "", "", ""},
            {"phone-live", "", "", "window.__spLive()"},
        }, false);
    }

    /** Rows made costly to raster (a phone's GPU is far slower than this emulator's host CPU), with and
     *  without messages arriving during the fling. */
    @Test public void heavyRowsLiveAtPhoneDensity() throws Exception {
        String heavy = ".cc-message-body{filter:drop-shadow(0 0 3px #3f3) drop-shadow(0 0 6px #0a0)!important}";
        room(true, new String[][] {
            {"heavy-static", heavy, "", ""},
            {"heavy-live", heavy, "", "window.__spLive()"},
        }, false);
    }

    @Test public void channelSwitchAtPhoneDensity() throws Exception {
        room(true, new String[0][], true);
    }

    /** `phone`: a 1080x2160 480dpi screen (360x720 CSS px at 3x, a real phone's raster load) instead of
     *  CI's 320x640 at 1x. */
    private void room(boolean phone, String[][] variants, boolean switching) throws Exception {
        /* A MEASUREMENT, run when named (scripts/emulator_tests_on_nas.sh -c ConcordScrollPaintDeviceTest): four cases
         * of ~25 s each would add minutes to CI's whole-suite run. The gate's check of the same behaviour is
         * tests/client/test_concord_live_messages_keep_the_scroller_full_app.py. */
        String asked = String.valueOf(InstrumentationRegistry.getArguments().getString("class"));
        Assume.assumeTrue("the scroll paint probe runs only when its class is named", asked.contains("ConcordScrollPaintDeviceTest"));
        if (phone) { RealUi.shell("wm size 1080x2160"); RealUi.shell("wm density 480"); SystemClock.sleep(2500); }
        ActivityScenario<MainActivity> scenario = ActivityScenario.launch(MainActivity.class);
        try {
            AtomicReference<WebView> ref = new AtomicReference<WebView>();
            WebView web = null;
            for (int i = 0; i < 80 && web == null; i++) {
                scenario.onActivity(a -> ref.set(findWebView(a.findViewById(android.R.id.content))));
                web = ref.get(); if (web == null) SystemClock.sleep(100);
            }
            assertTrue("MainActivity never created its WebView", web != null);
            String stable = "";
            for (int i = 0; i < 120; i++) {
                String now = eval(web, "location.href+'|'+document.readyState+'|'+!!window.PCConcord");
                if (now.contains("|complete|true") && now.equals(stable)) break;
                stable = now; SystemClock.sleep(100);
            }
            assertTrue("bundled Concord client never became ready: " + stable, stable.contains("|complete|true"));

            eval(web, SEED);
            String ready = "";
            for (int i = 0; i < 150; i++) {
                ready = eval(web, "window.__sp||''");
                if (ready.contains("ready") || ready.contains("error")) break;
                SystemClock.sleep(100);
            }
            assertTrue("Concord room never painted its history: " + ready, ready.contains("ready"));
            SystemClock.sleep(2500);   // enterChatBottom's pin-to-bottom passes end at 1.6 s
            eval(web, PROBE);

            if (switching) { switches(web); return; }
            StringBuilder summary = new StringBuilder();
            for (String[] v : variants) {
                double[] worst = measure(web, v);
                summary.append(v[0]).append(": worst=").append(Math.round(worst[0] * 100)).append("% mean=")
                        .append(Math.round(worst[1] * 100)).append("% blankFrames=").append((int) worst[2])
                        .append('/').append((int) worst[3]).append("; ");
            }
            Log.i(TAG, "SUMMARY " + summary);
        } finally {
            scenario.close();
            if (phone) { RealUi.shell("wm size reset"); RealUi.shell("wm density reset"); SystemClock.sleep(2500); }
        }
    }

    private int[] pane(WebView web) throws Exception {
        JSONArray g = new JSONArray(eval(web, "(()=>{const b=window.__spBox(),r=b.getBoundingClientRect();"
                + "return [r.left,r.top,r.width,r.height,devicePixelRatio]})()"));
        final int[] loc = new int[2];
        final WebView w = web;
        CountDownLatch l = new CountDownLatch(1);
        w.post(() -> { w.getLocationOnScreen(loc); l.countDown(); });
        l.await(5, TimeUnit.SECONDS);
        double dpr = g.getDouble(4);
        return new int[] {
            (int) Math.ceil(loc[0] + g.getDouble(0) * dpr) + 2,
            (int) Math.ceil(loc[1] + g.getDouble(1) * dpr) + 2,
            (int) Math.floor(loc[0] + (g.getDouble(0) + g.getDouble(2)) * dpr) - 2,
            (int) Math.floor(loc[1] + (g.getDouble(1) + g.getDouble(3)) * dpr) - 2,
            (int) Math.round(48 * dpr),
        };
    }

    /** {worst blank fraction, mean blank fraction, frames with blank >5%, frames} over two flings,
     *  each started from the newest message so the flings of every variant are alike. */
    private double[] measure(WebView web, String[] v) throws Exception {
        String name = v[0];
        eval(web, "(()=>{let s=document.getElementById('pcsp');if(!s){s=document.createElement('style');s.id='pcsp';"
                + "document.head.appendChild(s);}s.textContent=" + org.json.JSONObject.quote(v[1]) + ";return true;})()");
        UiAutomation ui = InstrumentationRegistry.getInstrumentation().getUiAutomation();
        double worst = 0, sum = 0; int frames = 0, bad = 0;
        for (int f = 0; f < 2; f++) {
            eval(web, "(()=>{const b=window.__spBox();b.scrollTop=b.scrollHeight;return true;})()");
            SystemClock.sleep(1200);
            int[] p = pane(web);
            int x0 = p[0], y0 = p[1], x1 = p[2], y1 = p[3], band = p[4];
            if (f == 0) {
                int[] rest = blank(ui.takeScreenshot(), x0, y0, x1, y1, band);
                Log.i(TAG, name + " rest pane=" + x0 + "," + y0 + "-" + x1 + "," + y1 + " blankRows=" + rest[0]
                        + " maxRun=" + rest[1]);
            }
            eval(web, "window.__spProbe.start()");
            if (!v[2].isEmpty()) eval(web, v[2]);
            fling(ui, (x0 + x1) / 2f, y0 + (y1 - y0) * 0.15f, y0 + (y1 - y0) * 0.85f);
            long t0 = SystemClock.uptimeMillis();
            StringBuilder line = new StringBuilder();
            long next = 150;
            while (SystemClock.uptimeMillis() - t0 < 1800) {
                long at = SystemClock.uptimeMillis() - t0;
                if (!v[3].isEmpty() && at >= next) { eval(web, "setTimeout(()=>{" + v[3] + "},0),1"); next = at + 150; }
                int[] b = blank(ui.takeScreenshot(), x0, y0, x1, y1, band);
                double frac = b[0] / (double) (y1 - y0);
                worst = Math.max(worst, frac); sum += frac; frames++;
                if (frac > 0.05) bad++;
                line.append(Math.round(frac * 100)).append(' ');
                if (b[0] > 0) Log.i(TAG, name + " fling" + f + " t=" + at + "ms blank=" + Math.round(frac * 100)
                        + "% profile=" + profileOf(b, y1 - y0));
            }
            Log.i(TAG, name + " fling" + f + " " + eval(web, "window.__spProbe.stop()") + " blank%=" + line);
        }
        return new double[] {worst, frames == 0 ? 0 : sum / frames, bad, frames};
    }

    /** Switch between two channels of 260 messages through the channel list, the way a phone does it:
     *  the drawer button, then the channel. Logs how long the pane stays blank and the long tasks. */
    private void switches(WebView web) throws Exception {
        UiAutomation ui = InstrumentationRegistry.getInstrumentation().getUiAutomation();
        String[] order = {"random", "general", "random", "general"};
        for (String ch : order) {
            int[] p = pane(web);
            eval(web, "window.__spProbe.start()");
            String r = eval(web, "(()=>{const t0=performance.now();document.querySelector('#cc-back-channels').click();"
                    + "const c=[...document.querySelectorAll('[data-cc-channel=\"" + ch + "\"]')].find(x=>x.getClientRects().length);"
                    + "if(!c)return 'no channel button';const t1=performance.now();c.click();"
                    + "return 'drawer='+Math.round(t1-t0)+'ms click='+Math.round(performance.now()-t1)+'ms';})()");
            long t0 = SystemClock.uptimeMillis();
            StringBuilder line = new StringBuilder();
            long firstPainted = -1;
            while (SystemClock.uptimeMillis() - t0 < 2500) {
                long at = SystemClock.uptimeMillis() - t0;
                int[] b = blank(ui.takeScreenshot(), p[0], p[1], p[2], p[3], p[4]);
                double frac = b[0] / (double) (p[3] - p[1]);
                line.append(at).append(':').append(Math.round(frac * 100)).append(' ');
                if (frac < 0.05 && firstPainted < 0) firstPainted = at;
            }
            Log.i(TAG, "switch->" + ch + " " + r + " painted@" + firstPainted + "ms "
                    + eval(web, "window.__spProbe.stop()") + " rows=" + eval(web,
                    "window.__spBox()?window.__spBox().querySelectorAll('.cc-message').length:-1") + " blank%=" + line);
            SystemClock.sleep(800);
        }
    }

    /** A fast downward swipe (scrolls toward OLDER messages) through the touchscreen input path. */
    private static void fling(UiAutomation ui, float x, float yFrom, float yTo) {
        long down = SystemClock.uptimeMillis();
        inject(ui, MotionEvent.obtain(down, down, MotionEvent.ACTION_DOWN, x, yFrom, 0));
        int steps = 6;
        for (int i = 1; i <= steps; i++) {
            SystemClock.sleep(10);
            float y = yFrom + (yTo - yFrom) * i / steps;
            inject(ui, MotionEvent.obtain(down, SystemClock.uptimeMillis(), MotionEvent.ACTION_MOVE, x, y, 0));
        }
        inject(ui, MotionEvent.obtain(down, SystemClock.uptimeMillis(), MotionEvent.ACTION_UP, x, yTo, 0));
    }

    private static void inject(UiAutomation ui, MotionEvent e) {
        e.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        ui.injectInputEvent(e, true);
        e.recycle();
    }

    /**
     * {blank rows, longest flat run, then one flag per row}: a row is FLAT when every sampled pixel is one
     * colour; a row is BLANK when it sits in a flat run at least `band` tall -- taller than the padding
     * between two painted messages, i.e. an area with nothing drawn in it.
     */
    static int[] blank(Bitmap shot, int x0, int y0, int x1, int y1, int band) {
        Bitmap bm = shot.getConfig() == Bitmap.Config.HARDWARE ? shot.copy(Bitmap.Config.ARGB_8888, false) : shot;
        int w = x1 - x0, h = y1 - y0;
        int[] px = new int[w * h];
        bm.getPixels(px, 0, w, x0, y0, w, h);
        boolean[] flat = new boolean[h];
        for (int y = 0; y < h; y++) {
            int c0 = px[y * w];
            boolean same = true;
            for (int x = 1; x < w && same; x += 2) {
                int c = px[y * w + x];
                same = Math.abs(((c >> 16) & 255) - ((c0 >> 16) & 255)) <= 10
                        && Math.abs(((c >> 8) & 255) - ((c0 >> 8) & 255)) <= 10
                        && Math.abs((c & 255) - (c0 & 255)) <= 10;
            }
            flat[y] = same;
        }
        int[] out = new int[2 + h];
        int run = 0;
        for (int y = 0; y <= h; y++) {
            if (y < h && flat[y]) { run++; continue; }
            out[1] = Math.max(out[1], run);
            if (run >= band) { out[0] += run; for (int k = y - run; k < y; k++) out[2 + k] = 1; }
            run = 0;
        }
        if (bm != shot) bm.recycle();
        shot.recycle();
        return out;
    }

    /** 32 characters, top to bottom: '#' where the pane is blank, '.' where something is painted. */
    static String profileOf(int[] b, int h) {
        StringBuilder s = new StringBuilder();
        for (int i = 0; i < 32; i++) {
            int from = i * h / 32, to = (i + 1) * h / 32, n = 0;
            for (int y = from; y < to; y++) n += b[2 + y];
            s.append(n * 2 > (to - from) ? '#' : '.');
        }
        return s.toString();
    }

    /** A local room with two channels of 260 messages long enough to wrap, entered the way a phone
     *  enters it (channel tap). */
    static final String SEED = "(()=>{window.__sp='pending';"
            + "const v0=__PC.viewer,pk=(v0().pubkey)||'d'.repeat(64);"
            + "if(!v0().pubkey)__PC.viewer=()=>Object.assign(v0(),{pubkey:pk,npub:'npub1devicetest'});"
            + "const RK='pc.concord.rooms.v1.'+pk,ID='device-scroll-paint';"
            + "localStorage.setItem(RK,JSON.stringify([{name:'Scroll paint',naddr:ID,local:true,"
            + "channels:[{name:'general',private:false},{name:'random',private:false,id:'random'}]}]));"
            + "localStorage.setItem('pc.concord.active.v1.'+pk,'0');"
            + "const words='the relay answered and the room kept every message we sent over the long weekend while somebody fixed the build again'.split(' ');"
            + "for(const [key,tag] of [[ID,'g'],[ID+'.random','r']]){const msgs=[],t0=Date.now()-300*60000;"
            + "for(let i=0;i<260;i++){const n=8+(i*7)%40,s=[];for(let j=0;j<n;j++)s.push(words[(i*3+j)%words.length]);"
            + "msgs.push({id:'sp'+tag+i,by:'Member '+(i%9),pubkey:String(i%9).repeat(64),at:t0+i*60000,text:'#'+tag+i+' '+s.join(' ')});}"
            + "localStorage.setItem('pc.concord.test.'+key,JSON.stringify(msgs));}"
            + "window.__spBox=()=>[...document.querySelectorAll('.cc-messages')].find(b=>{const r=b.getBoundingClientRect();"
            + "return r.width>1&&r.height>1;});"
            + "if(window.PCOS&&PCOS.mobileLanding)PCOS.mobileLanding();__PC.switchView('concord');"
            + "let tries=0;const go=()=>{if(!__PC.isView('concord'))__PC.switchView('concord');"
            + "const box=window.__spBox(),rows=box?box.querySelectorAll('.cc-message').length:0;"
            + "if(!box||rows<200){if(!box){const ch=document.querySelector('[data-cc-channel=\"general\"]');if(ch)ch.click();}"
            + "if(tries++<80){setTimeout(go,150);return;}window.__sp='error:rows='+rows+' box='+!!box;return;}"
            + "window.__sp='ready:'+rows;};go();return true;})()";

    /** Long tasks, the longest gap between frames, the largest single-frame scroll jump, and whether the
     *  scroller and the rows on screen were REPLACED while the room was flung. Also `__spLive()`: one
     *  message arriving in the open channel and the repaint a live room does for it. */
    static final String PROBE = "(()=>{const S={};window.__spProbe={start(){S.on=true;S.long=[];S.gap=0;S.jump=0;S.n=0;S.live=0;"
            + "S.last=performance.now();const b=window.__spBox();S.box=b;S.rows=b?[...b.querySelectorAll('.cc-message')]:[];"
            + "S.top=b?b.scrollTop:0;S.top0=S.top;"
            + "try{S.po=new PerformanceObserver(l=>{for(const e of l.getEntries())S.long.push(Math.round(e.duration));});"
            + "S.po.observe({type:'longtask'});}catch(_){}"
            + "const t=n=>{if(!S.on)return;const b=window.__spBox(),top=b?b.scrollTop:0;S.gap=Math.max(S.gap,n-S.last);"
            + "S.jump=Math.max(S.jump,Math.abs(top-S.top));S.top=top;S.last=n;S.n++;requestAnimationFrame(t);};requestAnimationFrame(t);"
            + "return true;},stop(){S.on=false;try{S.po.disconnect();}catch(_){}const b=window.__spBox();"
            + "return 'moved='+Math.round(S.top0-(b?b.scrollTop:0))+'px frames='+S.n+' maxGap='+Math.round(S.gap)+'ms maxJump='"
            + "+Math.round(S.jump)+'px live='+S.live+' scrollerReplaced='+(b!==S.box)+' rowsReplaced='"
            + "+S.rows.filter(r=>!r.isConnected).length+'/'+S.rows.length+' long=['+S.long.join(',')+']';}};"
            + "let n=1000;window.__spLive=()=>{const K='pc.concord.test.device-scroll-paint',a=JSON.parse(localStorage.getItem(K)||'[]');"
            + "a.push({id:'sp'+n,by:'Member '+(n%9),pubkey:String(n%9).repeat(64),at:Date.now(),text:'#'+n+' live message arriving mid fling'});n++;"
            + "localStorage.setItem(K,JSON.stringify(a));S.live++;PCConcord.backgroundRender();return true;};return true;})()";

    private static WebView findWebView(View view) {
        if (view instanceof WebView) return (WebView) view;
        if (!(view instanceof ViewGroup)) return null;
        ViewGroup group = (ViewGroup) view;
        for (int i = 0; i < group.getChildCount(); i++) {
            WebView found = findWebView(group.getChildAt(i)); if (found != null) return found;
        }
        return null;
    }

    private static String eval(WebView web, String js) throws Exception {
        CountDownLatch done = new CountDownLatch(1);
        AtomicReference<String> result = new AtomicReference<String>("null");
        web.post(() -> web.evaluateJavascript(js, value -> { result.set(value); done.countDown(); }));
        assertTrue("WebView did not answer JavaScript", done.await(15, TimeUnit.SECONDS));
        return result.get();
    }
}
