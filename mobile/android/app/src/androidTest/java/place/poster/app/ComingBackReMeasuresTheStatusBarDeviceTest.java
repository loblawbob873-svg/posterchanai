package place.poster.app;

import static org.junit.Assert.assertTrue;

import android.os.Build;
import android.os.SystemClock;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowInsets;
import android.webkit.WebView;

import androidx.lifecycle.Lifecycle;
import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;

import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.concurrent.atomic.AtomicReference;

/**
 * "Ouch man, it go back! ... when I close the app and enter again, it go back to normal." The page's
 * clearance below the status bar was re-measured only when insets reached the WebView or its own layout
 * changed, so one measurement taken while the bar was momentarily hidden stuck until a restart. Coming
 * back to the app -- pause then resume -- must measure again, and the page must end up below the bar.
 */
@RunWith(AndroidJUnit4.class)
public final class ComingBackReMeasuresTheStatusBarDeviceTest {

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
    public void aResumeMeasuresTheStatusBarAgainAndThePageEndsUpBelowIt() {
        if (Build.VERSION.SDK_INT < 30) return;               // WindowInsets.Type.statusBars() is API 30+
        try (ActivityScenario<MainActivity> sc = ActivityScenario.launch(MainActivity.class)) {
            SystemClock.sleep(4000);
            sc.moveToState(Lifecycle.State.CREATED);          // away: another app, the photo picker
            SystemClock.sleep(500);
            int before = MainActivity.barChecks;
            sc.moveToState(Lifecycle.State.RESUMED);          // and back
            SystemClock.sleep(2000);
            int after = MainActivity.barChecks;
            AtomicReference<String> got = new AtomicReference<>("");
            AtomicReference<Boolean> below = new AtomicReference<>(false);
            sc.onActivity(a -> {
                View decor = a.getWindow().getDecorView();
                WebView wv = find(decor);
                WindowInsets in = decor.getRootWindowInsets();
                if (wv == null || in == null) { got.set("no WebView/insets"); return; }
                int[] at = new int[2];
                wv.getLocationOnScreen(at);
                int[] d = new int[2];
                decor.getLocationOnScreen(d);
                int statusBottom = d[1] + in.getInsets(WindowInsets.Type.statusBars()).top;
                got.set("page top " + at[1] + ", status bar ends " + statusBottom);
                below.set(at[1] >= statusBottom);
            });
            assertTrue("coming back to the app did not re-measure the status bar (checks " + before + " -> " + after + ")",
                    after > before);
            assertTrue("after coming back the page is under the status bar: " + got.get(), below.get());
        }
    }
}
