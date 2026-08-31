package place.poster.app.push;

import static org.junit.Assert.assertEquals;

import android.app.NotificationManager;
import android.content.Context;
import android.os.SystemClock;
import android.service.notification.StatusBarNotification;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.Test;
import org.junit.runner.RunWith;

/** Distinct gCompat events coexist, while the same event id replaces its own shade card. */
@RunWith(AndroidJUnit4.class)
public class NotificationListDeviceTest {

    @Test
    public void distinctEventsAreNotCollapsedToOneNotification() {
        Context ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        NotificationManager nm = (NotificationManager)
                ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        for (int i = 0; i < 7; i++) {
            PushEventService.show(ctx, "Event " + i, "body", "",
                    "device-list-event-" + i, "post:event-" + i);
        }
        // Re-delivery of event 3 must update it, not create an eighth card.
        PushEventService.show(ctx, "Event 3 updated", "body", "",
                "device-list-event-3", "post:event-3");

        /* COUNT WHEN THE SHADE HAS CAUGHT UP, NOT THE INSTANT AFTER notify().
         *
         * `NotificationManager.notify` is asynchronous — it crosses a binder into system_server and
         * `getActiveNotifications()` reads what has LANDED. Reading immediately made this test a
         * race against a loaded CI emulator, and it lost: "expected:<7> but was:<6>", one card still
         * in flight, reported as a red build with nothing wrong in the app.
         *
         * Waiting does not weaken the assertion, which is the point. If the events really did
         * collapse the count stays below 7 for the whole window and this still fails; if a
         * re-delivery stacked an eighth card the count goes ABOVE 7, which the loop exits on and the
         * assert below catches. Only the timing is removed. */
        int ours = 0;
        for (int wait = 0; wait < 50; wait++) {
            ours = 0;
            for (StatusBarNotification n : nm.getActiveNotifications()) {
                if (n.getTag() != null && n.getTag().startsWith("device-list-event-")) ours++;
            }
            if (ours >= 7) break;
            SystemClock.sleep(100);
        }
        assertEquals("distinct pushes collapsed or a duplicate event stacked", 7, ours);
        for (int i = 0; i < 7; i++) nm.cancel("device-list-event-" + i, 1002);
    }
}
