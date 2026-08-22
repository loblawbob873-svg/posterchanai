package place.poster.app.sms;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.app.Instrumentation;
import android.app.NotificationManager;
import android.content.Context;
import android.service.notification.StatusBarNotification;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;

/**
 * A NEW TEXT IS ACTUALLY ANNOUNCED — measured on the device, from the notification shade.
 *
 * "make sure notifications work on new text messages" ... "otherwise useless", and it was exactly
 * that. On Android 13+ POST_NOTIFICATIONS is a runtime grant and `NotificationManager.notify` does
 * NOTHING without it: no error, no log, the message correctly stored by SmsDeliverReceiver and the
 * thread list correctly drawn. The messages half never declared the permission and never asked for
 * it — music, screen sharing and push each did, for their own flows, so somebody who had never
 * opened the player and never turned push on had never been asked.
 *
 * That is not visible from a source file. `notify()` returns void and succeeds whether or not
 * anything appears. The only proof is reading the shade back, which is what this does.
 *
 * The grant is taken through the shell (`pm grant`, the instrumentation runs as the shell UID).
 * Do not revoke it between cases: Android kills an app when a runtime permission is revoked, and
 * the instrumentation lives in that app process. The old teardown therefore passed the first case,
 * killed its own runner, and reported the next case as an empty "Process crashed" failure. CI uses
 * a freshly installed APK and disposable emulator, so retaining the grant for this test run cannot
 * leak into a user's install or another run. When it cannot be taken the tests SKIP with a reason
 * rather than passing: a check that could not run is not a check that passed.
 */
@RunWith(AndroidJUnit4.class)
public class SmsNotifyDeviceTest {

    private static final String PERM = "android.permission.POST_NOTIFICATIONS";

    private Context ctx;
    private boolean granted;

    @Before
    public void setUp() {
        ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        if (android.os.Build.VERSION.SDK_INT >= 33) {
            shell("pm grant " + ctx.getPackageName() + " " + PERM);
            granted = ctx.checkSelfPermission(PERM)
                    == android.content.pm.PackageManager.PERMISSION_GRANTED;
        } else {
            granted = true;                       // no runtime grant existed before 33
        }
        clearOurs();
    }

    @Test
    public void anIncomingTextReachesTheShade() throws Exception {
        org.junit.Assume.assumeTrue("could not take POST_NOTIFICATIONS on this device", granted);

        long when = System.currentTimeMillis();
        String body = "pc-notify-test-" + when;
        SmsNotifier.incoming(ctx, "+15550142", body, when, 4242L);

        StatusBarNotification found = waitForOurs(body);
        assertNotNull("a text was delivered and nothing appeared in the shade — this is the"
                + " \"notifications don't work on new text messages\" report, measured", found);
        assertEquals("it went to the wrong channel, so silencing texts would silence something else",
                SmsNotifier.CHANNEL, found.getNotification().getChannelId());
    }

    @Test
    public void itCarriesAReplyAndAMarkReadWithoutOpeningTheApp() throws Exception {
        org.junit.Assume.assumeTrue("could not take POST_NOTIFICATIONS on this device", granted);

        long when = System.currentTimeMillis();
        String body = "pc-notify-actions-" + when;
        SmsNotifier.incoming(ctx, "+15550143", body, when, 4243L);

        StatusBarNotification n = waitForOurs(body);
        assertNotNull("nothing appeared in the shade", n);
        android.app.Notification.Action[] acts = n.getNotification().actions;
        assertNotNull("a message notification with no actions is a tap target and nothing else", acts);
        assertTrue("expected a reply and a mark-read action, found " + acts.length, acts.length >= 2);
        // The inline reply is what makes it answerable from the lock screen; without a RemoteInput
        // the button opens the app, which is the thing it exists to avoid.
        boolean inline = false;
        for (android.app.Notification.Action a : acts) {
            if (a.getRemoteInputs() != null && a.getRemoteInputs().length > 0) inline = true;
        }
        assertTrue("no inline reply — the notification cannot be answered from the shade", inline);
    }

    @Test
    public void theChannelIsNotSilentByDefault() {
        // A message channel created at IMPORTANCE_LOW makes no sound and no heads-up, which reads as
        // "notifications don't work" just as completely as none at all.
        SmsNotifier.ensureChannel(ctx);
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        assertNotNull(nm);
        android.app.NotificationChannel c = nm.getNotificationChannel(SmsNotifier.CHANNEL);
        assertNotNull("the SMS channel was never created", c);
        assertTrue("the SMS channel is quieter than a message channel should be: importance "
                + c.getImportance(), c.getImportance() >= NotificationManager.IMPORTANCE_DEFAULT);
    }

    // ------------------------------------------------------------------ plumbing

    private StatusBarNotification waitForOurs(String body) throws Exception {
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return null;
        for (int i = 0; i < 40; i++) {
            for (StatusBarNotification s : nm.getActiveNotifications()) {
                CharSequence t = s.getNotification().extras.getCharSequence(
                        android.app.Notification.EXTRA_TEXT);
                if (t != null && body.contentEquals(t)) return s;
            }
            Thread.sleep(100);
        }
        return null;
    }

    private void clearOurs() {
        NotificationManager nm = (NotificationManager) ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm != null) try { nm.cancelAll(); } catch (Throwable ignored) { }
    }

    private static String shell(String cmd) {
        Instrumentation in = InstrumentationRegistry.getInstrumentation();
        try (InputStream is = new android.os.ParcelFileDescriptor.AutoCloseInputStream(
                in.getUiAutomation().executeShellCommand(cmd))) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = is.read(buf)) > 0) out.write(buf, 0, n);
            return out.toString("UTF-8");
        } catch (Throwable t) {
            return "shell failed: " + t;
        }
    }
}
