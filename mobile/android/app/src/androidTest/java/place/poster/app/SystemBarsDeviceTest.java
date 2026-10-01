package place.poster.app;

import static org.junit.Assert.assertTrue;

import android.os.Build;
import android.os.SystemClock;
import android.view.WindowInsets;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;

import org.junit.After;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.concurrent.atomic.AtomicReference;

/**
 * Settings -> Phone -> "Hide system bars" hides them ON THE REAL WINDOW, keeps them hidden across a
 * restart of the Activity, and shows them again when switched off ("any way to hide OS dock?", Samsung
 * tablet). Measured from the decor view's insets, not from the stored setting.
 */
@RunWith(AndroidJUnit4.class)
public final class SystemBarsDeviceTest {

    private static String bars(ActivityScenario<MainActivity> sc) {
        AtomicReference<String> got = new AtomicReference<>("?");
        sc.onActivity(a -> {
            WindowInsets in = a.getWindow().getDecorView().getRootWindowInsets();
            if (in == null) { got.set("no insets"); return; }
            got.set("status=" + in.isVisible(WindowInsets.Type.statusBars())
                    + " nav=" + in.isVisible(WindowInsets.Type.navigationBars()));
        });
        return got.get();
    }

    @After
    public void restore() {
        try (ActivityScenario<MainActivity> sc = ActivityScenario.launch(MainActivity.class)) {
            sc.onActivity(a -> SystemBars.setWanted(a, false));
        }
    }

    @Test
    public void hidingTheBarsHidesThemSurvivesARestartAndShowingBringsThemBack() {
        if (Build.VERSION.SDK_INT < 30) return;   // WindowInsets.isVisible is API 30+
        try (ActivityScenario<MainActivity> sc = ActivityScenario.launch(MainActivity.class)) {
            SystemClock.sleep(2000);
            sc.onActivity(a -> { SystemBars.setWanted(a, true); SystemBars.apply(a); });
            SystemClock.sleep(1500);
            String hidden = bars(sc);
            assertTrue("bars still visible after hiding: " + hidden, hidden.contains("status=false"));

            sc.recreate();                                    // rotation / renderer recovery
            SystemClock.sleep(2500);
            String again = bars(sc);
            assertTrue("bars came back after the Activity restarted: " + again, again.contains("status=false"));

            sc.onActivity(a -> { SystemBars.setWanted(a, false); SystemBars.apply(a); });
            SystemClock.sleep(1500);
            String shown = bars(sc);
            assertTrue("bars did not come back when switched off: " + shown, shown.contains("status=true"));
        }
    }
}
