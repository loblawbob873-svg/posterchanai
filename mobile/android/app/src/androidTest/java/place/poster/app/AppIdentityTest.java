package place.poster.app;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import android.content.Context;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.pm.ProviderInfo;
import android.util.Log;

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
    public void theAppUnderTestIsOurs() throws Exception {
        Context ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        assertEquals("place.poster.app", ctx.getPackageName());
        // Inspect installed, merged manifests: source-only checks miss a library initializer
        // reintroduced by a build variant or the separate instrumentation APK.
        checkStartup(ctx, true);
        checkStartup(InstrumentationRegistry.getInstrumentation().getContext(), false);
        logEmojiState("before first Activity");
    }

    private static void checkStartup(Context ctx, boolean requireProvider) throws Exception {
        PackageInfo info = ctx.getPackageManager().getPackageInfo(ctx.getPackageName(),
                PackageManager.GET_PROVIDERS | PackageManager.GET_META_DATA);
        boolean found = false;
        if (info.providers != null) for (ProviderInfo provider : info.providers) {
            if (!"androidx.startup.InitializationProvider".equals(provider.name)) continue;
            found = true;
            Log.i("PosterChan", "Installed startup metadata " + ctx.getPackageName()
                    + " authority=" + provider.authority + " metadata=" + provider.metaData);
            assertTrue("Installed " + ctx.getPackageName() + " enables downloadable emoji fonts",
                    provider.metaData == null || !provider.metaData.containsKey(
                            "androidx.emoji2.text.EmojiCompatInitializer"));
        }
        Log.i("PosterChan", "Installed startup provider " + ctx.getPackageName() + " present=" + found);
        assertTrue("Main APK lost its other AndroidX startup initializers", !requireProvider || found);
    }

    static void logEmojiState(String phase) {
        try {
            Class<?> emoji = Class.forName("androidx.emoji2.text.EmojiCompat");
            Log.i("PosterChan", "EmojiCompat " + phase + " configured="
                    + emoji.getMethod("isConfigured").invoke(null));
        } catch (ReflectiveOperationException error) {
            Log.i("PosterChan", "EmojiCompat " + phase + " inspection=" + error);
        }
    }
}
