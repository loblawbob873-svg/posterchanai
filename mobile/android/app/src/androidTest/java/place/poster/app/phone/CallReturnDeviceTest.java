package place.poster.app.phone;

import static org.junit.Assert.*;

import android.app.Activity;
import android.app.Instrumentation;
import android.app.Notification;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.telecom.Call;
import android.view.View;

import androidx.lifecycle.Lifecycle;
import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.Test;
import org.junit.runner.RunWith;
import java.util.Collections;
import java.util.List;
import place.poster.app.R;

/** Real Android views/notification builder/lifecycle and tap intents; no carrier call is placed. */
@RunWith(AndroidJUnit4.class)
public class CallReturnDeviceTest {
    private Context ctx() { return InstrumentationRegistry.getInstrumentation().getTargetContext(); }

    // Only the presence of live calls is supplied. Rendering, lifecycle and click listeners are
    // production code; Android Telecom remains untouched and no number can be dialled by this test.
    private static final class Calls extends PcInCallService {
        boolean active;
        @Override public List<Call> liveCalls() {
            return active ? Collections.singletonList((Call) null) : Collections.emptyList();
        }
    }

    private Instrumentation.ActivityMonitor watchCallScreen() {
        return InstrumentationRegistry.getInstrumentation().addMonitor(InCallActivity.class.getName(),
                new Instrumentation.ActivityResult(Activity.RESULT_CANCELED, new Intent()), true);
    }

    @Test public void returnControlSurvivesLeavingPhoneAndDoesNotDependOnNotifications() {
        PcInCallService previous = PcInCallService.INSTANCE;
        Calls calls = new Calls(); calls.active = true; PcInCallService.INSTANCE = calls;
        Instrumentation.ActivityMonitor monitor = watchCallScreen();
        try (ActivityScenario<DialerActivity> s = ActivityScenario.launch(new Intent(ctx(), DialerActivity.class))) {
            s.onActivity(a -> assertEquals(View.VISIBLE, a.findViewById(R.id.pc_dl_return_call).getVisibility()));
            s.moveToState(Lifecycle.State.CREATED); // another app owns the screen
            s.moveToState(Lifecycle.State.RESUMED);
            s.onActivity(a -> {
                View button = a.findViewById(R.id.pc_dl_return_call);
                assertEquals(View.VISIBLE, button.getVisibility());
                assertTrue(button.performClick());
            });
            assertEquals("tap did not return to the native call controls", 1, monitor.getHits());
        } finally {
            PcInCallService.INSTANCE = previous;
            InstrumentationRegistry.getInstrumentation().removeMonitor(monitor);
        }
    }

    @Test public void callEndingWhileAwayRemovesReturnControlAndStaleTapDoesNotLaunch() {
        PcInCallService previous = PcInCallService.INSTANCE;
        Calls calls = new Calls(); calls.active = true; PcInCallService.INSTANCE = calls;
        Instrumentation.ActivityMonitor monitor = watchCallScreen();
        try (ActivityScenario<DialerActivity> s = ActivityScenario.launch(new Intent(ctx(), DialerActivity.class))) {
            // A call can end between rendering the button and tapping it.
            s.onActivity(a -> { calls.active = false; a.findViewById(R.id.pc_dl_return_call).performClick(); });
            assertEquals(0, monitor.getHits());
            s.moveToState(Lifecycle.State.CREATED);
            s.moveToState(Lifecycle.State.RESUMED);
            s.onActivity(a -> assertEquals(View.GONE, a.findViewById(R.id.pc_dl_return_call).getVisibility()));
        } finally {
            PcInCallService.INSTANCE = previous;
            InstrumentationRegistry.getInstrumentation().removeMonitor(monitor);
        }
    }

    @Test public void noCallMeansNoReturnControl() {
        PcInCallService previous = PcInCallService.INSTANCE; PcInCallService.INSTANCE = null;
        try (ActivityScenario<DialerActivity> s = ActivityScenario.launch(new Intent(ctx(), DialerActivity.class))) {
            s.onActivity(a -> assertEquals(View.GONE, a.findViewById(R.id.pc_dl_return_call).getVisibility()));
        } finally { PcInCallService.INSTANCE = previous; }
    }

    @Test public void ongoingAndRingingNotificationsUseCallStyleWithWorkingReturnIntent() throws Exception {
        InCallNotifier.ensureChannels(ctx());
        for (int state : new int[]{CallRules.STATE_ACTIVE, CallRules.STATE_HOLDING, CallRules.STATE_DIALING, CallRules.STATE_RINGING}) {
            Notification n = InCallNotifier.build(ctx(), state, "Test caller");
            assertEquals(Notification.CATEGORY_CALL, n.category);
            assertTrue((n.flags & Notification.FLAG_ONGOING_EVENT) != 0);
            assertNotNull(n.contentIntent);
            assertEquals(state == CallRules.STATE_RINGING ? 2 : 1, n.actions.length);
            if (Build.VERSION.SDK_INT >= 31) {
                assertEquals("android.app.Notification$CallStyle", n.extras.getString("android.template"));
            }
            if (state == CallRules.STATE_RINGING) assertNotNull(n.fullScreenIntent);
            else assertTrue(n.fullScreenIntent == null);
            PendingIntent expected = PendingIntent.getActivity(ctx(), 0,
                    new Intent(ctx(), InCallActivity.class)
                            .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP),
                    PendingIntent.FLAG_NO_CREATE | PendingIntent.FLAG_IMMUTABLE);
            assertNotNull("no pending route to native call controls", expected);
            assertEquals(expected, n.contentIntent);
        }
    }
}
