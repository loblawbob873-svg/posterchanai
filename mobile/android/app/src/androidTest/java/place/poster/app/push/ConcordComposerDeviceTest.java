package place.poster.app.push;

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

/** Proves the shipped APK's real Chromium textarea survives Concord's asynchronous repaint path. */
@RunWith(AndroidJUnit4.class)
public class ConcordComposerDeviceTest {
    @Test public void draftSelectionAndFocusSurviveThreeWholeWorkspaceRepaints() throws Exception {
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
                if ((now.contains("https://localhost/") || now.contains("capacitor://localhost/")) &&
                        now.contains("|complete|true") && now.equals(stable)) break;
                stable = now; SystemClock.sleep(100);
            }
            assertTrue("bundled Concord client never became ready: " + stable,
                    stable.contains("|complete|true"));

            String js = "(()=>{window.__ccDeviceResult='pending';"+
                    "const old=localStorage.getItem('pc.concord.invites');"+
                    "localStorage.setItem('pc.concord.invites',JSON.stringify([{name:'Device draft gate',"+
                    "naddr:'device-draft-gate',local:true,channels:[{name:'general',private:false}]}]));"+
                    "localStorage.setItem('pc.concord.active','0');"+
                    // Earlier device tests deliberately exercise desktop mode. MainActivity and its
                    // WebView survive between those tests, so switchView would correctly route the
                    // request into a desktop window while this test looked in the classic feed. Use
                    // the same supported transition phoneshell.js uses when Android opens the mobile
                    // app; this is a mobile composer test, not a hidden-desktop-window test.
                    "if(window.PCOS&&PCOS.mobileLanding)PCOS.mobileLanding();__PC.switchView('concord');"+
                    "let tries=0;const seed=()=>{const a=document.querySelector('#cc-input');"+
                    "if(!a&&tries++<50){setTimeout(seed,100);return;}"+
                    "try{if(!a)throw new Error('composer absent; view='+__PC.isView('concord')+"+
                    "', desktop='+(!!(window.PCOS&&PCOS.isOn&&PCOS.isOn())));"+
                    "a.value='draft survives repaint';a.focus();a.setSelectionRange(6,14,'backward');"+
                    "PCConcord.render();PCConcord.render();PCConcord.render();requestAnimationFrame(()=>{"+
                    "requestAnimationFrame(()=>{const b=document.querySelector('#cc-input');"+
                    "const before={value:b&&b.value,start:b&&b.selectionStart,end:b&&b.selectionEnd,"+
                    "focused:document.activeElement===b,replaced:a!==b};"+
                    // A restoration latch must not become a focus trap. Move focus to another real
                    // control after one more replacement but before its rAF callback.
                    "PCConcord.render();const target=document.querySelector('#cc-emoji');target.focus();"+
                    "requestAnimationFrame(()=>{requestAnimationFrame(()=>{window.__ccDeviceResult="+
                    "JSON.stringify({...before,notStolen:document.activeElement===target});"+
                    "if(old===null)localStorage.removeItem('pc.concord.invites');else localStorage.setItem('pc.concord.invites',old);"+
                    "});});});});}catch(e){if(old===null)localStorage.removeItem('pc.concord.invites');"+
                    "else localStorage.setItem('pc.concord.invites',old);window.__ccDeviceResult='error:'+e;}};"+
                    "seed();return true;})()";
            eval(web, js);
            String result = "";
            for (int i = 0; i < 80; i++) {
                result = eval(web, "window.__ccDeviceResult||''");
                if (!result.contains("pending")) break;
                SystemClock.sleep(100);
            }
            assertTrue("APK Concord repaint lost draft state: " + result,
                    result.contains("draft survives repaint") && result.contains("\\\"start\\\":6") &&
                    result.contains("\\\"end\\\":14") && result.contains("\\\"focused\\\":true") &&
                    result.contains("\\\"replaced\\\":true") &&
                    result.contains("\\\"notStolen\\\":true"));
        } finally { scenario.close(); }
    }

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
