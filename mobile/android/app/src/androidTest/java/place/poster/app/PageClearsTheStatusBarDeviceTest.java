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

    /**
     * THE GALAXY S25 CASE: One UI 8 drew the window under the status bar AND the WebView's insets
     * listener was never handed the bars, so the first fix (margin = the insets delivered) did nothing
     * and the search sat under the clock. Reproduced here: the window forced edge-to-edge, the
     * listener starved. The WebView must still end up below the status bar -- measured, not told.
     */
    @Test
    public void theWebViewClearsTheStatusBarEvenWhenItsInsetsListenerIsHandedNothing() {
        try (ActivityScenario<MainActivity> sc = ActivityScenario.launch(MainActivity.class)) {
            SystemClock.sleep(3000);
            sc.onActivity(a -> {
                androidx.core.view.WindowCompat.setDecorFitsSystemWindows(a.getWindow(), false);
                WebView wv = find(a.getWindow().getDecorView());
                if (wv != null) {
                    androidx.core.view.ViewCompat.setOnApplyWindowInsetsListener(wv,
                            (v, insets) -> androidx.core.view.WindowInsetsCompat.CONSUMED);
                    ViewGroup.LayoutParams raw = wv.getLayoutParams();
                    if (raw instanceof ViewGroup.MarginLayoutParams) {
                        ((ViewGroup.MarginLayoutParams) raw).setMargins(0, 0, 0, 0);
                        wv.setLayoutParams(raw);
                    }
                    wv.requestLayout();
                }
            });
            SystemClock.sleep(2000);
            AtomicReference<String> got = new AtomicReference<>("");
            AtomicReference<Boolean> ok = new AtomicReference<>(false);
            sc.onActivity(a -> {
                View decor = a.getWindow().getDecorView();
                WebView wv = find(decor);
                if (wv == null) { got.set("no WebView"); return; }
                int[] at = new int[2];
                wv.getLocationOnScreen(at);
                WindowInsets root = decor.getRootWindowInsets();
                int statusBottom = root == null ? 0 : (Build.VERSION.SDK_INT >= 30
                        ? root.getInsetsIgnoringVisibility(WindowInsets.Type.statusBars()).top
                        : root.getStableInsetTop());
                got.set("webview top=" + at[1] + " status bar bottom=" + statusBottom);
                ok.set(statusBottom > 0 && at[1] >= statusBottom);
            });
            assertTrue("edge-to-edge with a starved listener: the page is under the status bar: " + got.get(), ok.get());
        }
    }
}
