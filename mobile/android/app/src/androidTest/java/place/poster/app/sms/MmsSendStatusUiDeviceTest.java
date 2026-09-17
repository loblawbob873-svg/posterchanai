package place.poster.app.sms;

import static org.junit.Assert.*;
import static androidx.test.espresso.Espresso.onView;
import static androidx.test.espresso.matcher.ViewMatchers.withText;
import static androidx.test.espresso.matcher.ViewMatchers.withId;
import static androidx.test.espresso.matcher.RootMatchers.isDialog;
import static androidx.test.espresso.action.ViewActions.click;
import static androidx.test.espresso.matcher.ViewMatchers.isDisplayed;
import static androidx.test.espresso.assertion.ViewAssertions.matches;

import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.widget.TextView;
import androidx.test.core.app.ActivityScenario;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.junit.Test;
import org.junit.runner.RunWith;
import place.poster.app.R;
import java.io.ByteArrayOutputStream;

/** Real native rendering, private test draft only: never invokes a carrier send. */
@RunWith(AndroidJUnit4.class)
public class MmsSendStatusUiDeviceTest {
    /* A picture the private-link path could not send stays on the composer as NOT SENT, with its
     * reason one tap away: that is the one draft the person still has to act on. */
    @Test public void aFailedAttachmentShowsWhyAndDetailsRemainAccessible() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        String address = "+15550009421";
        String detail = "The upload could not reach your server.";
        try {
            MmsDraft.Value draft = MmsDraft.save(context, address, png(), "image/png", "photo.png");
            MmsDraft.state(context, draft.key, MmsDraft.FAILED, detail);
            Intent intent = new Intent(context, ThreadActivity.class)
                    .putExtra(ThreadActivity.EXTRA_ADDRESS, address);
            try (ActivityScenario<ThreadActivity> scenario = ActivityScenario.launch(intent)) {
                scenario.onActivity(activity -> {
                    TextView status = activity.findViewById(R.id.pc_th_attachment_status);
                    assertEquals("photo.png · Not sent\nTap for details", status.getText().toString());
                });
                // Drive the real tap through Espresso, then select the newly opened dialog root.
                onView(withId(R.id.pc_th_attachment_status)).perform(click());
                onView(withText(detail)).inRoot(isDialog()).check(matches(isDisplayed()));
            }
        } finally { MmsDraft.remove(context, address); }
    }

    /* A picture the carrier already took is not part of the next message. An earlier build left it
     * on the composer as "Send status unconfirmed", where send() refused every follow-up text. */
    @Test public void aPictureTheCarrierTookIsNotLeftOnTheComposer() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        String address = "+15550009422";
        try {
            MmsDraft.Value draft = MmsDraft.save(context, address, png(), "image/png", "photo.png");
            MmsDraft.state(context, draft.key, MmsDraft.UNKNOWN, MmsFailures.reason(5, 0));
            Intent intent = new Intent(context, ThreadActivity.class)
                    .putExtra(ThreadActivity.EXTRA_ADDRESS, address);
            try (ActivityScenario<ThreadActivity> scenario = ActivityScenario.launch(intent)) {
                scenario.onActivity(activity -> {
                    assertEquals(android.view.View.GONE,
                            activity.findViewById(R.id.pc_th_attachment_draft).getVisibility());
                });
            }
            assertNull("the finished draft is still stored", MmsDraft.load(context, address));
        } finally { MmsDraft.remove(context, address); }
    }

    private static byte[] png() {
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        Bitmap image = Bitmap.createBitmap(2, 2, Bitmap.Config.ARGB_8888);
        image.compress(Bitmap.CompressFormat.PNG, 100, bytes); image.recycle();
        return bytes.toByteArray();
    }
}
