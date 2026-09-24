package place.poster.app;

import static org.junit.Assert.assertTrue;

import android.os.Build;
import android.os.SystemClock;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.webkit.WebView;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;

import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.concurrent.atomic.AtomicReference;

/**
 * "Many things are cut off at the top" -- the Social search, Notes, the Terminal. The page must start
 * BELOW the status bar on every device, whether or not the system honours the themes' edge-to-edge
 * opt-out: measured as the WebView's top on screen against the bottom of the status bar.
 */
@RunWith(AndroidJUnit4.class)
public final class PageClearsTheStatusBarDeviceTest {

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
    public void theWebViewStartsBelowTheStatusBar() {
        try (ActivityScenario<MainActivity> sc = ActivityScenario.launch(MainActivity.class)) {
            SystemClock.sleep(3000);
            AtomicReference<String> got = new AtomicReference<>("");
            AtomicReference<Boolean> ok = new AtomicReference<>(false);
            sc.onActivity(a -> {
                View decor = a.getWindow().getDecorView();
                WebView wv = find(decor);
                if (wv == null) { got.set("no WebView"); return; }
                int[] at = new int[2];
                wv.getLocationOnScreen(at);
                int statusBottom = 0;
                WindowInsets root = decor.getRootWindowInsets();
                if (root != null) {
                    statusBottom = Build.VERSION.SDK_INT >= 30
                            ? root.getInsetsIgnoringVisibility(WindowInsets.Type.statusBars()).top
                            : root.getStableInsetTop();
                }
                got.set("webview top=" + at[1] + " status bar bottom=" + statusBottom);
                ok.set(at[1] >= statusBottom);
            });
            assertTrue("the page starts under the status bar: " + got.get(), ok.get());
        }
    }
}
