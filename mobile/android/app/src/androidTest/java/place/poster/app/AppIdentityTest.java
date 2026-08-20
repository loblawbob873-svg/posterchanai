package place.poster.app;

import static org.junit.Assert.assertEquals;

import android.content.Context;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.Test;
import org.junit.runner.RunWith;

/**
 * The floor: the app under test is the app we think it is.
 *
 * It replaces Capacitor's generated ExampleInstrumentedTest, which asserted the package was
 * `com.getcapacitor.app` — the template's id, never this app's. That test had sat here failing since
 * the project was created and nobody knew, because nothing had ever RUN the instrumented suite. The
 * moment the emulator workflow started running it, it would have failed the whole job for a reason
 * that has nothing to do with any feature.
 */
@RunWith(AndroidJUnit4.class)
public class AppIdentityTest {

    @Test
    public void theAppUnderTestIsOurs() {
        Context ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        assertEquals("place.poster.app", ctx.getPackageName());
    }
}
