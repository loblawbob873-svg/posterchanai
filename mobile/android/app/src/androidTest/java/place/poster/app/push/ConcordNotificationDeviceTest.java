package place.poster.app.push;

import static org.junit.Assert.assertEquals;

import android.content.Context;
import android.content.Intent;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.Test;
import org.junit.runner.RunWith;

import place.poster.app.MainActivity;
import place.poster.app.home.HomeActivity;

/** The packaged notification intent must retain every Concord target on a real Android runtime. */
@RunWith(AndroidJUnit4.class)
public class ConcordNotificationDeviceTest {
    @Test public void roomChannelAndMessageSurviveTheNotificationTapIntent() {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        String route = "concord:community%3Aalpha:support:message%3A42";
        long at = System.currentTimeMillis();
        Intent intent = PushEventService.openIntent(context, route, at);

        assertEquals(MainActivity.class.getName(), intent.getComponent().getClassName());
        assertEquals(route, intent.getStringExtra(HomeActivity.EXTRA_VIEW));
        assertEquals(at, intent.getLongExtra(HomeActivity.EXTRA_VIEW_AT, 0));
        assertEquals(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP,
                intent.getFlags());
    }
}
