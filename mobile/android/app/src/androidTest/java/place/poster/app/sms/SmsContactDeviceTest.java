package place.poster.app.sms;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.app.Activity;
import android.app.Instrumentation;
import android.content.Intent;
import android.provider.ContactsContract;
import android.view.View;

import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.Test;
import org.junit.runner.RunWith;

import java.util.concurrent.atomic.AtomicReference;

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
        try (ActivityScenario<ThreadActivity> scenario = ActivityScenario.launch(launch)) {
            // Install interception only AFTER ThreadActivity is RESUMED. A blocking filter monitor
            // registered before ActivityScenario.launch was itself observed in startActivitySync's
            // wait for 90 seconds. This callback form captures just the outgoing editor intent and
            // returns a result synchronously, without starting or waiting on the external Contacts
            // activity (which is outside this app and outside this assertion).
            AtomicReference<Intent> captured = new AtomicReference<Intent>();
            Instrumentation.ActivityMonitor monitor = new Instrumentation.ActivityMonitor() {
                @Override public Instrumentation.ActivityResult onStartActivity(Intent intent) {
                    if (!Intent.ACTION_INSERT.equals(intent.getAction())) return null;
                    captured.set(new Intent(intent));
                    return new Instrumentation.ActivityResult(Activity.RESULT_CANCELED, null);
                }
            };
            instrumentation.addMonitor(monitor);
            try {
                scenario.onActivity(activity -> {
                    View add = activity.findViewById(R.id.pc_th_add_contact);
                    // An emulator address book can be empty or inaccessible. The action must remain
                    // reachable for the unknown number in either case.
                    assertTrue("Texts hid Add contact for an unknown SMS sender",
                            add.getVisibility() == View.VISIBLE);
                    assertTrue("Add contact did not accept the tap", add.performClick());
                });
                Intent editor = captured.get();
                assertNotNull("Add contact did not launch the platform contact editor", editor);
                assertEquals(ContactsContract.Contacts.CONTENT_URI, editor.getData());
                assertEquals(number,
                        editor.getStringExtra(ContactsContract.Intents.Insert.PHONE));
            } finally {
                instrumentation.removeMonitor(monitor);
            }
        }
    }
}
