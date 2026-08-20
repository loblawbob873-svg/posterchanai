package place.poster.app.weather;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.content.ComponentName;
import android.content.Context;
import android.content.pm.PackageManager;
import android.view.View;
import android.view.ViewGroup;
import android.widget.RemoteViews;
import android.widget.TextView;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.After;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * THE WEATHER WIDGET, APPLIED FOR REAL.
 *
 * A widget's RemoteViews are inflated by the LAUNCHER's process, which supports a fixed handful of
 * view types and will simply refuse anything else — and the refusal is the classic grey box, with
 * nothing in this app's log to say why. Nothing off a device can see that: the layout parses, the
 * ids resolve, and the compile is green either way.
 *
 * So this APPLIES the RemoteViews and reads the text back out. It never touches the network — the
 * emulator has no PosterChan instance to ask — which makes it a test of exactly the state a person
 * sees first: a freshly placed widget that has not been told where they are.
 */
@RunWith(AndroidJUnit4.class)
public class WeatherWidgetDeviceTest {

    private Context ctx;

    @Before
    public void setUp() {
        ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        WeatherStore.sp(ctx).edit().clear().commit();
    }

    @After
    public void tearDown() {
        WeatherStore.sp(ctx).edit().clear().commit();
    }

    @Test
    public void theProviderAndItsPickerAreBothInstalled() {
        PackageManager pm = ctx.getPackageManager();
        try {
            assertNotNull("the weather provider is not in the manifest",
                    pm.getReceiverInfo(new ComponentName(ctx, WeatherWidget.class), 0));
            // The configuration activity is what makes "tap to set your location" reachable at all.
            // A widget whose `android:configure` names something not installed lands as a grey box.
            assertNotNull(pm.getActivityInfo(
                    new ComponentName(ctx, WeatherConfigActivity.class), 0));
        } catch (PackageManager.NameNotFoundException e) {
            throw new AssertionError("a weather component named in the manifest is not installed", e);
        }
    }

    @Test
    public void aFreshWidgetSaysWhatToDoRatherThanDrawingAnEmptyBox() throws Exception {
        final View[] out = new View[1];
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
            RemoteViews rv = WeatherWidget.build(ctx);
            // apply() is the launcher's own path. A view type RemoteViews will not carry, or an id
            // that is not in the layout it names, throws HERE and nowhere else.
            out[0] = rv.apply(ctx, new android.widget.FrameLayout(ctx));
        });
        assertNotNull("the weather widget would not inflate", out[0]);
        String text = allText(out[0]);
        assertTrue("a widget with no location must say so, not sit blank: [" + text + "]",
                text.contains("Tap to set your location"));
        assertTrue("no placeholder temperature", text.contains("—"));
    }

    @Test
    public void aReadingIsDrawnAndAnOldOneIsLabelled() throws Exception {
        WeatherStore.setPlace(ctx, 51.5, -0.12, "London");
        WeatherStore.setServer(ctx, "https://example.invalid", "metric");
        WeatherStore.setReading(ctx, 7.4, 61, true, 9.0, 3.0, "°C",
                System.currentTimeMillis() - 5L * 60 * 60 * 1000);
        final View[] out = new View[1];
        InstrumentationRegistry.getInstrumentation().runOnMainSync(
                () -> out[0] = WeatherWidget.build(ctx).apply(ctx, new android.widget.FrameLayout(ctx)));
        String text = allText(out[0]);
        assertTrue("no temperature: [" + text + "]", text.contains("7°C"));
        assertTrue("no condition: [" + text + "]", text.contains("Rain"));
        assertTrue("no place: [" + text + "]", text.toUpperCase().contains("LONDON"));
        // THE HONESTY LINE. Five hours old is past the point where presenting it as now is a lie.
        assertTrue("a five-hour-old reading was drawn as current: [" + text + "]",
                text.contains("5h ago"));
    }

    @Test
    public void aFailedFetchLeavesTheLastRealReadingAlone() {
        // The reason it does not go blank on a train. `example.invalid` cannot resolve, so this is a
        // real failed fetch against a real network stack, not a mocked one.
        WeatherStore.setPlace(ctx, 51.5, -0.12, "London");
        WeatherStore.setServer(ctx, "https://example.invalid", "metric");
        long at = System.currentTimeMillis() - 60000;
        WeatherStore.setReading(ctx, 7.4, 61, true, 9.0, 3.0, "°C", at);
        assertEquals(false, WeatherFetch.refresh(ctx));
        assertEquals("the failed fetch moved the timestamp", at, WeatherStore.at(ctx));
        assertEquals(Double.valueOf(7.4), WeatherStore.temp(ctx));
    }

    @Test
    public void withNoInstanceItMakesNoRequestAtAll() {
        WeatherStore.setPlace(ctx, 51.5, -0.12, "London");
        WeatherStore.setServer(ctx, "", "metric");
        assertEquals(false, WeatherFetch.refresh(ctx));
        assertEquals(0, WeatherFetch.search(ctx, "london").length());
    }

    private static String allText(View v) {
        StringBuilder b = new StringBuilder();
        collect(v, b);
        return b.toString();
    }

    private static void collect(View v, StringBuilder b) {
        if (v instanceof TextView) b.append(((TextView) v).getText()).append(' ');
        if (!(v instanceof ViewGroup)) return;
        ViewGroup g = (ViewGroup) v;
        for (int i = 0; i < g.getChildCount(); i++) collect(g.getChildAt(i), b);
    }
}
