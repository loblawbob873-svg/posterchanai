package place.poster.app;

import static org.junit.Assert.assertTrue;

import android.content.Context;
import android.os.Build;
import android.os.SystemClock;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.view.inputmethod.InputMethodManager;
import android.webkit.WebView;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;

import org.junit.Assume;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.concurrent.atomic.AtomicReference;

/**
 * "The terminal top gets cut off, where you see the terminal tabs" -- with the keyboard up. MainActivity
 * never said how to make room for the keyboard, so Android PANNED the whole window up to keep the
 * focused field visible, sliding the page's top under the status bar. With the keyboard open the page
 * must still start below the status bar AND end above the keyboard: it shrinks, it never slides.
 */
@RunWith(AndroidJUnit4.class)
public final class KeyboardNeverSlidesThePageDeviceTest {

    private static WebView find(View v) {
        if (v instanceof WebView) return (WebView) v;
        if (v instanceof ViewGroup) {
            ViewGroup g = (ViewGroup) v;
            for (int i = 0; i < g.getChildCount(); i++) {
                WebView w = find(g.getChildAt(i));
                if (w != null) return w;
            }
        }
        return null;
    }

    @Test
    public void withTheKeyboardUpThePageStaysBelowTheStatusBarAndAboveTheKeyboard() {
        if (Build.VERSION.SDK_INT < 30) return;               // WindowInsets.Type.ime() is API 30+
        try (ActivityScenario<MainActivity> sc = ActivityScenario.launch(MainActivity.class)) {
            SystemClock.sleep(4000);
            // A field at the very BOTTOM of the page -- the case the system pans for.
            sc.onActivity(a -> {
                WebView wv = find(a.getWindow().getDecorView());
                if (wv == null) return;
                wv.evaluateJavascript("(()=>{let i=document.getElementById('pcKbProbe');if(!i){i=document.createElement('input');"
                        + "i.id='pcKbProbe';i.style.cssText='position:fixed;left:8px;bottom:8px;width:200px;height:40px;z-index:2147483647';"
                        + "document.body.appendChild(i);}i.focus();return document.activeElement===i;})()", null);
                wv.requestFocus();
                InputMethodManager imm = (InputMethodManager) a.getSystemService(Context.INPUT_METHOD_SERVICE);
                if (imm != null) imm.showSoftInput(wv, InputMethodManager.SHOW_IMPLICIT);
            });
            boolean up = false;
            for (int i = 0; i < 40 && !up; i++) {
                SystemClock.sleep(250);
                AtomicReference<Boolean> vis = new AtomicReference<>(false);
                sc.onActivity(a -> {
                    WindowInsets in = a.getWindow().getDecorView().getRootWindowInsets();
                    vis.set(in != null && in.isVisible(WindowInsets.Type.ime()));
                });
                up = vis.get();
            }
            Assume.assumeTrue("no software keyboard appeared on this device -- nothing to measure", up);
            SystemClock.sleep(1500);                           // let the resize settle
            AtomicReference<String> got = new AtomicReference<>("");
            AtomicReference<Boolean> ok = new AtomicReference<>(false);
            sc.onActivity(a -> {
                View decor = a.getWindow().getDecorView();
                WebView wv = find(decor);
                WindowInsets in = decor.getRootWindowInsets();
                if (wv == null || in == null) { got.set("no WebView/insets"); return; }
                int[] at = new int[2];
                wv.getLocationOnScreen(at);
                int[] d = new int[2];
                decor.getLocationOnScreen(d);
                int statusBottom = d[1] + in.getInsetsIgnoringVisibility(WindowInsets.Type.statusBars()).top;
                int imeTop = d[1] + decor.getHeight() - in.getInsets(WindowInsets.Type.ime()).bottom;
                int top = at[1], bottom = at[1] + wv.getHeight();
                got.set("page " + top + ".." + bottom + " status bar ends " + statusBottom + " keyboard starts " + imeTop);
                ok.set(top >= statusBottom && bottom <= imeTop + 1);
            });
            assertTrue("with the keyboard up the page slid under the status bar or under the keyboard: "
                    + got.get(), ok.get());
        }
    }
}
