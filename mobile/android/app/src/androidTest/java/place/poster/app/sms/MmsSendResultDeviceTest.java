package place.poster.app.sms;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.database.Cursor;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.os.ParcelFileDescriptor;
import android.provider.Telephony;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.InputStream;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/** Real framework result handoff: goAsync clears BroadcastReceiver's result ownership.
 * No transport is invoked. Each case owns one synthetic provider row with no address,
 * attachment or PDU; an explicit local ordered broadcast supplies the carrier result.
 */
@RunWith(AndroidJUnit4.class)
public class MmsSendResultDeviceTest {
    @Test public void successfulCarrierResultSurvivesGoAsync() throws Exception {
        checkResult(Activity.RESULT_OK, Telephony.Mms.MESSAGE_BOX_SENT);
    }

    @Test public void carrierErrorSurvivesGoAsync() throws Exception {
        checkResult(8, Telephony.Mms.MESSAGE_BOX_FAILED); // mobile data unavailable
    }

    @Test public void genuinelyAmbiguousZeroRemainsInOutbox() throws Exception {
        checkResult(0, Telephony.Mms.MESSAGE_BOX_OUTBOX);
    }

    @Test public void providerHonorsFailedToOutboxCompareAndSet() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        String previous = Telephony.Sms.getDefaultSmsPackage(context);
        Uri row = null;
        try {
            shell("cmd role add-role-holder android.app.role.SMS " + context.getPackageName());
            assertEquals(context.getPackageName(), Telephony.Sms.getDefaultSmsPackage(context));
            ContentValues values = new ContentValues();
            values.put(Telephony.Mms.MESSAGE_BOX, Telephony.Mms.MESSAGE_BOX_FAILED);
            values.put(Telephony.Mms.DATE, System.currentTimeMillis() / 1000);
            row = context.getContentResolver().insert(Telephony.Mms.CONTENT_URI, values);
            assertNotNull(row);
            assertEquals(Telephony.Mms.MESSAGE_BOX_FAILED, box(context, row));
            ContentValues pending = new ContentValues();
            pending.put(Telephony.Mms.MESSAGE_BOX, Telephony.Mms.MESSAGE_BOX_OUTBOX);
            String selection = Telephony.Mms.MESSAGE_BOX + "=?";
            String[] failed = {String.valueOf(Telephony.Mms.MESSAGE_BOX_FAILED)};
            assertEquals("First retry claims exactly its failed provider row", 1,
                    context.getContentResolver().update(row, pending, selection, failed));
            assertEquals(Telephony.Mms.MESSAGE_BOX_OUTBOX, box(context, row));
            assertEquals("Provider must honor selection and refuse a second retry claim", 0,
                    context.getContentResolver().update(row, pending, selection, failed));
            assertEquals(Telephony.Mms.MESSAGE_BOX_OUTBOX, box(context, row));
        } finally {
            if (row != null) context.getContentResolver().delete(row, null, null);
            if (previous != null && !previous.equals(context.getPackageName())) {
                shell("cmd role add-role-holder android.app.role.SMS " + previous);
                assertEquals(previous, Telephony.Sms.getDefaultSmsPackage(context));
            } else if (previous == null) {
                shell("cmd role remove-role-holder android.app.role.SMS " + context.getPackageName());
            }
        }
    }

    private void checkResult(int carrierResult, int expectedBox) throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        String previous = Telephony.Sms.getDefaultSmsPackage(context);
        Uri row = null;
        long id = 0;
        try {
            shell("cmd role add-role-holder android.app.role.SMS " + context.getPackageName());
            assertEquals("Device must grant the SMS role to exercise the real provider",
                    context.getPackageName(), Telephony.Sms.getDefaultSmsPackage(context));
            ContentValues values = new ContentValues();
            values.put(Telephony.Mms.MESSAGE_BOX, Telephony.Mms.MESSAGE_BOX_OUTBOX);
            values.put(Telephony.Mms.DATE, System.currentTimeMillis() / 1000);
            values.put(Telephony.Mms.READ, 1);
            values.put(Telephony.Mms.SEEN, 1);
            row = context.getContentResolver().insert(Telephony.Mms.CONTENT_URI, values);
            assertNotNull("Synthetic MMS provider row", row);
            id = Long.parseLong(row.getLastPathSegment());
            assertEquals(Telephony.Mms.MESSAGE_BOX_OUTBOX, box(context, row));

            CountDownLatch finished = new CountDownLatch(1);
            Intent result = new Intent(context, MmsSendReceiver.class)
                    .setAction(MmsSendReceiver.ACTION_SENT)
                    .putExtra("content_uri", row.toString());
            // A real delivered broadcast supplies the PendingResult. Calling onReceive directly
            // or stubbing getResultCode would miss the framework ownership regression entirely.
            context.sendOrderedBroadcast(result, null, new BroadcastReceiver() {
                @Override public void onReceive(Context ignored, Intent delivered) {
                    finished.countDown();
                }
            }, new Handler(Looper.getMainLooper()), carrierResult, null, null);
            assertTrue("Async MMS receiver must finish its broadcast", finished.await(8, TimeUnit.SECONDS));
            assertEquals("Carrier result must survive goAsync()", expectedBox, box(context, row));
            if (carrierResult == Activity.RESULT_OK) {
                assertEquals("Successful result must not create an unknown-delivery error", "",
                        MmsFailures.get(context, id));
            } else if (carrierResult == 8) {
                assertEquals("mobile data is unavailable", MmsFailures.get(context, id));
            } else {
                assertTrue(MmsFailures.indeterminate(MmsFailures.get(context, id)));
            }
        } finally {
            if (row != null) context.getContentResolver().delete(row, null, null);
            if (id > 0) MmsFailures.clear(context, id);
            if (previous != null && !previous.equals(context.getPackageName())) {
                shell("cmd role add-role-holder android.app.role.SMS " + previous);
                assertEquals("Restore the previous SMS role holder", previous,
                        Telephony.Sms.getDefaultSmsPackage(context));
            } else if (previous == null) {
                shell("cmd role remove-role-holder android.app.role.SMS " + context.getPackageName());
            }
        }
    }

    private static int box(Context context, Uri row) {
        try (Cursor cursor = context.getContentResolver().query(row,
                new String[]{Telephony.Mms.MESSAGE_BOX}, null, null, null)) {
            assertNotNull(cursor);
            assertTrue("Synthetic MMS row still exists", cursor.moveToFirst());
            return cursor.getInt(0);
        }
    }

    private static void shell(String command) throws Exception {
        try (ParcelFileDescriptor descriptor = InstrumentationRegistry.getInstrumentation()
                .getUiAutomation().executeShellCommand(command);
             InputStream input = new ParcelFileDescriptor.AutoCloseInputStream(descriptor)) {
            byte[] buffer = new byte[1024];
            while (input.read(buffer) != -1) { /* Wait for role manager completion. */ }
        }
    }
}
