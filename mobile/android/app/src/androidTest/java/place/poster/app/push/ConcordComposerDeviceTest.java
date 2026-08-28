package place.poster.app.push;

import static org.junit.Assert.assertTrue;

import android.os.SystemClock;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebView;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;

import org.junit.Test;
import org.junit.runner.RunWith;
import org.json.JSONArray;

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

            // JavaScript focus() is ignored when the WebView itself does not own Android window
            // focus (notably after launcher/lifecycle tests). Establish the real native precondition
            // first; otherwise this test asks backgroundRender to protect an unfocused textarea and
            // reports the expected rebuild as a product failure.
            final WebView underTest = web;
            scenario.onActivity(a -> {
                underTest.requestFocus(View.FOCUS_DOWN);
                underTest.requestFocusFromTouch();
            });
            String nativeFocus = "";
            for (int i = 0; i < 30; i++) {
                nativeFocus = eval(web, "document.hasFocus()+'|'+document.visibilityState");
                if (nativeFocus.contains("true|visible")) break;
                SystemClock.sleep(100);
            }
            assertTrue("WebView never received foreground focus: " + nativeFocus,
                    nativeFocus.contains("true|visible"));

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
                    "let tries=0;const seed=()=>{"+
                    // A preceding tablet/desktop lifecycle callback can finish its Classic landing
                    // after our first switch. Do not mistake that test-order race for a missing
                    // composer: require the real Concord route and re-enter it until it is stable.
                    "if(!__PC.isView('concord')){__PC.switchView('concord');"+
                    "if(tries++<50){setTimeout(seed,100);return;}throw new Error('Concord route did not stay active');}"+
                    "const inputs=[...document.querySelectorAll('#cc-input')],"+
                    "a=inputs.find(x=>{const q=x.getBoundingClientRect();return q.width>1&&q.height>1;})||inputs[inputs.length-1];"+
                    "if(!a&&tries++<50){setTimeout(seed,100);return;}"+
                    "try{if(!a)throw new Error('composer absent; view='+__PC.isView('concord')+"+
                    "', desktop='+(!!(window.PCOS&&PCOS.isOn&&PCOS.isOn())));"+
                    // Selecting a community intentionally leaves a phone on its channel list. Its
                    // hidden conversation already has a textarea, but a finger cannot reach it.
                    // Enter #general through the real channel control and reacquire the textarea
                    // after that click's render replacement before seeding the draft.
                    "const r=a.getBoundingClientRect();if(r.width<=1||r.height<=1){"+
                    "const app=a.closest('.cc-app'),channel=app&&app.querySelector('[data-cc-channel=\"general\"]');"+
                    // A delayed lifecycle landing may replace the workspace after a successful
                    // click. Channel selection is idempotent, so open #general in whichever current
                    // workspace owns this hidden composer on every retry rather than latching once.
                    "if(channel){channel.click();setTimeout(seed,100);return;}"+
                    "if(tries++<50){setTimeout(seed,100);return;}throw new Error('composer stayed hidden');}"+
                    "a.value='draft survives repaint';window.__ccDeviceOldInvites=old;"+
                    "window.__ccDeviceResult='ready-for-touch';"+
                    "}catch(e){if(old===null)localStorage.removeItem('pc.concord.invites');"+
                    "else localStorage.setItem('pc.concord.invites',old);window.__ccDeviceResult='error:'+e;}};"+
                    "seed();return true;})()";
            eval(web, js);
            String ready = "";
            for (int i = 0; i < 80; i++) {
                ready = eval(web, "window.__ccDeviceResult||''");
                if (ready.contains("ready-for-touch") || ready.contains("error:")) break;
                SystemClock.sleep(100);
            }
            assertTrue("APK Concord composer was not ready for native input: " + ready,
                    ready.contains("ready-for-touch"));

            // A JavaScript focus() is not a user gesture. Chromium may correctly refuse it even
            // while document.hasFocus() is true, which made the old test fail before exercising the
            // product path. Tap the textarea through Android's real WebView input dispatcher—the
            // same path a finger and the soft keyboard use—then prove Chromium focused that node.
            JSONArray hit = new JSONArray(eval(web,
                    "(()=>{const a=document.querySelector('#cc-input'),r=a.getBoundingClientRect();"
                    + "return [r.left+r.width/2,r.top+r.height/2,devicePixelRatio,r.width,r.height]})()"));
            assertTrue("Concord composer has no tappable rectangle: " + hit,
                    hit.getDouble(3) > 1 && hit.getDouble(4) > 1);
            final float tapX = (float) (hit.getDouble(0) * hit.getDouble(2));
            final float tapY = (float) (hit.getDouble(1) * hit.getDouble(2));
            scenario.onActivity(a -> {
                long at = SystemClock.uptimeMillis();
                MotionEvent down = MotionEvent.obtain(at, at, MotionEvent.ACTION_DOWN, tapX, tapY, 0);
                MotionEvent up = MotionEvent.obtain(at, at + 40, MotionEvent.ACTION_UP, tapX, tapY, 0);
                underTest.dispatchTouchEvent(down); underTest.dispatchTouchEvent(up);
                down.recycle(); up.recycle();
            });
            String focused = "";
            for (int i = 0; i < 30; i++) {
                focused = eval(web, "document.activeElement===document.querySelector('#cc-input')");
                if (focused.contains("true")) break;
                SystemClock.sleep(100);
            }
            assertTrue("native tap did not focus the Concord textarea: " + focused,
                    focused.contains("true"));

            eval(web, "(()=>{window.__ccDeviceResult='pending';const a=document.querySelector('#cc-input'),old=window.__ccDeviceOldInvites;"
                    + "try{a.setSelectionRange(6,14,'backward');const initiallyFocused=document.activeElement===a;"
                    + "PCConcord.backgroundRender();PCConcord.backgroundRender();PCConcord.backgroundRender();"
                    + "requestAnimationFrame(()=>{requestAnimationFrame(()=>{const b=document.querySelector('#cc-input');"
                    + "const before={value:b&&b.value,start:b&&b.selectionStart,end:b&&b.selectionEnd,"
                    + "initiallyFocused,focused:document.activeElement===b,replaced:a!==b};"
                    // A restoration latch must not become a focus trap. Move focus to another real
                    // control after one more replacement but before its rAF callback.
                    + "const target=document.querySelector('#cc-emoji');target.focus();PCConcord.backgroundRender();"
                    + "requestAnimationFrame(()=>{requestAnimationFrame(()=>{window.__ccDeviceResult="
                    + "JSON.stringify({...before,notStolen:document.activeElement===target});"
                    + "if(old===null)localStorage.removeItem('pc.concord.invites');else localStorage.setItem('pc.concord.invites',old);"
                    + "delete window.__ccDeviceOldInvites;});});});});}catch(e){"
                    + "if(old===null)localStorage.removeItem('pc.concord.invites');else localStorage.setItem('pc.concord.invites',old);"
                    + "delete window.__ccDeviceOldInvites;window.__ccDeviceResult='error:'+e;}})()");
            String result = "";
            for (int i = 0; i < 80; i++) {
                result = eval(web, "window.__ccDeviceResult||''");
                if (!result.contains("pending")) break;
                SystemClock.sleep(100);
            }
            assertTrue("APK Concord repaint lost draft state: " + result,
                    result.contains("draft survives repaint") && result.contains("\\\"start\\\":6") &&
                    result.contains("\\\"end\\\":14") && result.contains("\\\"focused\\\":true") &&
                    result.contains("\\\"initiallyFocused\\\":true") &&
                    result.contains("\\\"replaced\\\":false") &&
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
