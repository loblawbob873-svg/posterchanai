package place.poster.app.push;

import static org.junit.Assert.assertEquals;

import android.app.NotificationManager;
import android.content.Context;
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

        int ours = 0;
        for (StatusBarNotification n : nm.getActiveNotifications()) {
            if (n.getTag() != null && n.getTag().startsWith("device-list-event-")) ours++;
        }
        assertEquals("distinct pushes collapsed or a duplicate event stacked", 7, ours);
        for (int i = 0; i < 7; i++) nm.cancel("device-list-event-" + i, 1002);
    }
}
