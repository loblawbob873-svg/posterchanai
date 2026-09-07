package place.poster.app.sms;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.content.Context;
import android.content.ContextWrapper;
import android.content.Intent;
import android.text.Spanned;
import android.text.style.URLSpan;
import android.view.ContextThemeWrapper;
import android.view.LayoutInflater;
import android.view.MotionEvent;
import android.view.View;
import android.widget.TextView;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import org.junit.Test;
import org.junit.runner.RunWith;
import place.poster.app.R;

@RunWith(AndroidJUnit4.class)
public class SmsLinksDeviceTest {
    @Test
    public void tappingDownloadLinkOpensTheCompleteEncryptionFragment() {
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
            final Intent[] opened = new Intent[1];
            Context base = new ContextThemeWrapper(
                    InstrumentationRegistry.getInstrumentation().getTargetContext(),
                    android.R.style.Theme_Material_Light);
            Context context = new ContextWrapper(base) {
                @Override public void startActivity(Intent intent) { opened[0] = intent; }
            };
            TextView text = (TextView) LayoutInflater.from(base).cloneInContext(context)
                    .inflate(R.layout.sms_bubble, null).findViewById(R.id.pc_b_text);
            String link = "https://poster.place/f/" + new String(new char[64]).replace('\0', 'a')
                    + "#pcenc1=eyJrIjoiYWJjXy0xMjM0NTY3ODkiLCJjIjoxfQ";
            SmsLinks.bind(text, "Download:\n" + link);
            URLSpan[] spans = ((Spanned) text.getText()).getSpans(0, text.length(), URLSpan.class);
            assertEquals(1, spans.length);
            assertEquals(link, spans[0].getURL());
            assertTrue(text.isTextSelectable());
            text.measure(View.MeasureSpec.makeMeasureSpec(700, View.MeasureSpec.EXACTLY),
                    View.MeasureSpec.makeMeasureSpec(2000, View.MeasureSpec.AT_MOST));
            text.layout(0, 0, text.getMeasuredWidth(), text.getMeasuredHeight());
            int offset = ((Spanned) text.getText()).getSpanStart(spans[0]) + 4;
            int line = text.getLayout().getLineForOffset(offset);
            float x = text.getTotalPaddingLeft() + text.getLayout().getPrimaryHorizontal(offset);
            float y = text.getTotalPaddingTop() + (text.getLayout().getLineTop(line)
                    + text.getLayout().getLineBottom(line)) / 2f;
            MotionEvent down = MotionEvent.obtain(100, 100, MotionEvent.ACTION_DOWN, x, y, 0);
            MotionEvent up = MotionEvent.obtain(100, 150, MotionEvent.ACTION_UP, x, y, 0);
            try { text.onTouchEvent(down); text.onTouchEvent(up); }
            finally { down.recycle(); up.recycle(); }
            assertNotNull("Tapping the URL must open a browser intent", opened[0]);
            assertEquals(Intent.ACTION_VIEW, opened[0].getAction());
            assertEquals(link, opened[0].getDataString());
        });
    }

    @Test
    public void recycledPlainMessageHasNoOldLinkOrExecutableMarkup() {
        InstrumentationRegistry.getInstrumentation().runOnMainSync(() -> {
            TextView text = new TextView(InstrumentationRegistry.getInstrumentation().getTargetContext());
            SmsLinks.bind(text, "https://poster.place/f/example#pcenc1=key");
            SmsLinks.bind(text, "javascript:alert(1) <b>plain text</b>");
            assertEquals("javascript:alert(1) <b>plain text</b>", text.getText().toString());
            assertEquals(0, text.getUrls().length);
        });
    }
}
