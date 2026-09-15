package place.poster.app.sms;

import static org.junit.Assert.*;
import static androidx.test.espresso.Espresso.onView;
import static androidx.test.espresso.matcher.ViewMatchers.withText;
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
    @Test public void uncertainAttachmentIsQuietAndDetailsRemainAccessible() throws Exception {
        Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        String address = "+15550009421";
        ByteArrayOutputStream bytes = new ByteArrayOutputStream();
        Bitmap image = Bitmap.createBitmap(2, 2, Bitmap.Config.ARGB_8888);
        image.compress(Bitmap.CompressFormat.PNG, 100, bytes); image.recycle();
        String detail = MmsFailures.reason(5, 0);
        try {
            MmsDraft.Value draft = MmsDraft.save(context, address, bytes.toByteArray(), "image/png", "photo.png");
            MmsDraft.state(context, draft.key, MmsDraft.UNKNOWN, detail);
            Intent intent = new Intent(context, ThreadActivity.class)
                    .putExtra(ThreadActivity.EXTRA_ADDRESS, address);
            try (ActivityScenario<ThreadActivity> scenario = ActivityScenario.launch(intent)) {
                scenario.onActivity(activity -> {
                    TextView status = activity.findViewById(R.id.pc_th_attachment_status);
                    assertEquals("photo.png · Send status unconfirmed\nTap for details", status.getText().toString());
                    assertFalse(status.getText().toString().contains("I/O"));
                    assertTrue(status.performClick());
                });
                onView(withText(detail)).check(matches(isDisplayed()));
            }
        } finally { MmsDraft.remove(context, address); }
    }
}
