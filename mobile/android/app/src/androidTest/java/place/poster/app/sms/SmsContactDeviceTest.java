package place.poster.app.sms;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Intent;
import android.content.IntentFilter;
import android.provider.ContactsContract;
import android.view.View;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.Test;
import org.junit.runner.RunWith;

import place.poster.app.R;

/** Exercises the native Texts affordance on Android rather than merely matching Java source. */
@RunWith(AndroidJUnit4.class)
public class SmsContactDeviceTest {

    @Test
    public void addContactLaunchesAndroidsContactEditorWithTheSmsNumber() throws Exception {
        final String number = "+1 (555) 010-4477";
        Intent launch = new Intent(InstrumentationRegistry.getInstrumentation().getTargetContext(),
                ThreadActivity.class).putExtra(ThreadActivity.EXTRA_ADDRESS, number);
        Instrumentation instrumentation = InstrumentationRegistry.getInstrumentation();
        Instrumentation.ActivityMonitor monitor = new Instrumentation.ActivityMonitor(
                new IntentFilter(Intent.ACTION_INSERT), null, true);
        instrumentation.addMonitor(monitor);
        try (ActivityScenario<ThreadActivity> scenario = ActivityScenario.launch(launch)) {
            scenario.onActivity(activity -> {
                View add = activity.findViewById(R.id.pc_th_add_contact);
                // An emulator address book can be empty or inaccessible. The action must remain
                // reachable for the unknown number in either case.
                assertTrue("Texts hid Add contact for an unknown SMS sender",
                        add.getVisibility() == View.VISIBLE);
                add.performClick();
            });
            long deadline = System.currentTimeMillis() + 3000;
            while (monitor.getHits() == 0 && System.currentTimeMillis() < deadline) Thread.sleep(20);
            assertEquals("Add contact did not launch the platform contact editor", 1, monitor.getHits());
        } finally {
            instrumentation.removeMonitor(monitor);
        }
    }
}
