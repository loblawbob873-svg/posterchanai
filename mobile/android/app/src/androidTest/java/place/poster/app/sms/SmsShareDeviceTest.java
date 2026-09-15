package place.poster.app.sms;

import static org.junit.Assert.*;
import static androidx.test.espresso.Espresso.onView;
import static androidx.test.espresso.action.ViewActions.*;
import static androidx.test.espresso.matcher.ViewMatchers.*;
import static androidx.test.espresso.matcher.RootMatchers.isDialog;

import android.app.Instrumentation;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.widget.EditText;
import androidx.core.content.FileProvider;
import androidx.test.core.app.ActivityScenario;
import androidx.test.core.app.ApplicationProvider;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.io.File;
import java.io.FileOutputStream;
import org.junit.Test;
import org.junit.runner.RunWith;

/** Real Activity handoff/recreation and content-provider access; never presses Send. */
@RunWith(AndroidJUnit4.class)
public class SmsShareDeviceTest {
    private final Context ctx = ApplicationProvider.getApplicationContext();
    private final String who = "+15550987654";

    private Intent picture(String name, String caption) throws Exception {
        File dir = new File(ctx.getCacheDir(), "mms-camera"); dir.mkdirs();
        File file = new File(dir, name);
        try (FileOutputStream out = new FileOutputStream(file)) { out.write(name.getBytes("UTF-8")); }
        Uri uri = FileProvider.getUriForFile(ctx, ctx.getPackageName()+".fileprovider", file);
        Intent intent = new Intent(Intent.ACTION_SEND).setType("image/jpeg")
                .putExtra(Intent.EXTRA_STREAM, uri).putExtra(Intent.EXTRA_TEXT, caption)
                .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        intent.setClipData(android.content.ClipData.newRawUri("photo", uri));
        return intent;
    }

    private void awaitDraft(String name) throws Exception {
        long deadline = System.currentTimeMillis()+5000;
        while (System.currentTimeMillis()<deadline) {
            MmsDraft.Value draft = MmsDraft.load(ctx, who);
            if (draft != null && name.equals(draft.name)) {
                assertEquals(MmsDraft.READY, draft.state);
                assertArrayEquals(name.getBytes("UTF-8"), java.nio.file.Files.readAllBytes(draft.file.toPath()));
                return;
            }
            Thread.sleep(30);
        }
        fail("shared attachment never became a durable unsent draft");
    }

    @Test public void galleryShareAsksForRecipientAndStagesWithoutSending() throws Exception {
        MmsDraft.remove(ctx, who); MmsDraft.setText(ctx, who, "");
        Intent share = picture("shared-route.jpg", "Shared caption");
        // Some galleries include content data as well: it is never a phone number.
        share.setDataAndType(share.getParcelableExtra(Intent.EXTRA_STREAM), "image/jpeg");
        boolean resolves = false;
        for (android.content.pm.ResolveInfo info : ctx.getPackageManager().queryIntentActivities(share, 0))
            if (info.activityInfo.name.equals(SendToActivity.class.getName())) resolves = true;
        assertTrue("Messages must be offered by the gallery share sheet", resolves);
        Intent forwarded = new Intent(ctx, ThreadActivity.class);
        SmsShare.forward(share, forwarded);
        assertEquals(share.getParcelableExtra(Intent.EXTRA_STREAM), forwarded.getClipData().getItemAt(0).getUri());
        assertEquals(Intent.FLAG_GRANT_READ_URI_PERMISSION,
                forwarded.getFlags() & (Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION));
        Instrumentation inst = InstrumentationRegistry.getInstrumentation();
        Instrumentation.ActivityMonitor monitor = inst.addMonitor(ThreadActivity.class.getName(), null, false);
        ThreadActivity thread = null;
        try (ActivityScenario<SendToActivity> route = ActivityScenario.launch(share.setClass(ctx, SendToActivity.class))) {
            // The recipient field lives in an AlertDialog above the transparent routing Activity.
            // The default Activity root never gains focus while that dialog is open.
            onView(isAssignableFrom(EditText.class)).inRoot(isDialog()).perform(replaceText(who), closeSoftKeyboard());
            assertNull("recipient selection must not import/send yet", MmsDraft.load(ctx, who));
            onView(withId(android.R.id.button1)).inRoot(isDialog()).perform(click());
            thread = (ThreadActivity) inst.waitForMonitorWithTimeout(monitor, 5000);
            assertNotNull("recipient selection must open the conversation", thread);
            awaitDraft("shared-route.jpg");
            final ThreadActivity shown = thread;
            inst.runOnMainSync(() -> assertEquals("Shared caption", ((EditText)shown.findViewById(place.poster.app.R.id.pc_th_input)).getText().toString()));
        } finally {
            if (thread != null) { final ThreadActivity shown = thread; inst.runOnMainSync(shown::finish); }
            inst.removeMonitor(monitor); MmsDraft.remove(ctx, who); MmsDraft.setText(ctx, who, "");
        }
    }

    @Test public void cancelledReplacementKeepsOriginalPhotoAndEmptyCaption() throws Exception {
        MmsDraft.remove(ctx, who); MmsDraft.setText(ctx, who, "");
        MmsDraft.save(ctx, who, new byte[]{1, 2, 3}, "image/jpeg", "original.jpg");
        // ActivityScenario tracks the original Intent.filterEquals signature across recreation.
        // Start with the same media action/type as the later warm share (without a stream, so
        // the durable original remains untouched). Otherwise onNewIntent changes that signature
        // and the tracker ignores the replacement Activity's lifecycle events forever.
        Intent open = new Intent(ctx, ThreadActivity.class).putExtra(ThreadActivity.EXTRA_ADDRESS, who)
                .setAction(Intent.ACTION_SEND).setType("image/jpeg");
        try (ActivityScenario<ThreadActivity> scenario = ActivityScenario.launch(open)) {
            Intent replacement = new Intent(ctx, ThreadActivity.class).putExtra(ThreadActivity.EXTRA_ADDRESS, who)
                    .putExtra(Intent.EXTRA_TEXT, "caption for a different photo")
                    .setData(Uri.parse("smsto:" + who + "?body=caption%20from%20a%20photo%20link"));
            SmsShare.forward(picture("cancelled.jpg", ""), replacement);
            assertTrue("recreation must retain the scenario's Intent identity", open.filterEquals(replacement));
            scenario.onActivity(activity -> {
                activity.onNewIntent(replacement);
                assertEquals("", ((EditText)activity.findViewById(place.poster.app.R.id.pc_th_input)).getText().toString());
            });
            onView(withId(android.R.id.button2)).inRoot(isDialog()).perform(click());
            scenario.recreate();
            assertEquals("original.jpg", MmsDraft.load(ctx, who).name);
            scenario.onActivity(activity -> assertEquals("", ((EditText)activity.findViewById(place.poster.app.R.id.pc_th_input)).getText().toString()));
            assertEquals("", MmsDraft.text(ctx, who));
        } finally { MmsDraft.remove(ctx, who); MmsDraft.setText(ctx, who, ""); }
    }

    @Test public void realRecreationKeepsDraftAndWarmShareRequiresReplacementConfirmation() throws Exception {
        MmsDraft.remove(ctx, who); MmsDraft.setText(ctx, who, "");
        Intent first = new Intent(ctx, ThreadActivity.class).putExtra(ThreadActivity.EXTRA_ADDRESS, who);
        SmsShare.forward(picture("shared-first.jpg", ""), first);
        try (ActivityScenario<ThreadActivity> scenario = ActivityScenario.launch(first)) {
            awaitDraft("shared-first.jpg");
            scenario.recreate();
            scenario.onActivity(activity -> assertNull("recreation must not replay consumed share", SmsShare.stream(activity.getIntent())));
            awaitDraft("shared-first.jpg");
            Intent next = new Intent(ctx, ThreadActivity.class).putExtra(ThreadActivity.EXTRA_ADDRESS, who);
            SmsShare.forward(picture("shared-second.jpg", ""), next);
            scenario.onActivity(activity -> activity.onNewIntent(next));
            assertEquals("shared-first.jpg", MmsDraft.load(ctx, who).name);
            Intent newer = new Intent(ctx, ThreadActivity.class).putExtra(ThreadActivity.EXTRA_ADDRESS, who);
            SmsShare.forward(picture("shared-newest.jpg", ""), newer);
            scenario.onActivity(activity -> activity.onNewIntent(newer));
            onView(withId(android.R.id.button1)).inRoot(isDialog()).perform(click());
            awaitDraft("shared-newest.jpg");
            // The older dialog must not replace a newer share for the same recipient.
            onView(withId(android.R.id.button1)).inRoot(isDialog()).perform(click());
            Thread.sleep(100);
            awaitDraft("shared-newest.jpg");
        } finally { MmsDraft.remove(ctx, who); MmsDraft.setText(ctx, who, ""); }
    }
}
