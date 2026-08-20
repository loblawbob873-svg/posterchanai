package place.poster.app.sms;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;

/**
 * "TEXT THIS NUMBER", asked by another app.
 *
 * The third of the four things Android requires before an app may hold the SMS role: an activity for
 * ACTION_SENDTO on `sms:`, `smsto:`, `mms:` and `mmsto:`. It is what a `Text` link in a web page, a
 * contact card's message button and a share sheet all land on.
 *
 * It holds no UI of its own — it reads the URI (SendTo, which is pure and tested against the six
 * shapes these arrive in) and hands off to the conversation, then finishes so it never appears in
 * the back stack. Pressing back from the thread returns to whatever asked, which is what somebody
 * who tapped a link expects.
 *
 * A PLAIN Activity with a TRANSLUCENT theme, and both matter. It draws nothing, so it should SHOW
 * nothing: given an opaque theme the person tapping a link in a web page gets a black rectangle for
 * a frame before the conversation appears. And it extends Activity rather than PcActivity because it
 * has no palette to apply — there is no view to apply one to.
 */
public class SendToActivity extends Activity {

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        Intent in = getIntent();
        String to = SendTo.numberFrom(in == null ? null : in.getData());
        String body = SendTo.bodyFrom(in == null ? null : in.getData());
        if (body.isEmpty() && in != null) {
            // A share sheet sends the text as an extra rather than in the URI.
            CharSequence extra = in.getCharSequenceExtra(Intent.EXTRA_TEXT);
            if (extra != null) body = extra.toString();
        }
        Intent open = new Intent(this, ThreadActivity.class)
                .putExtra(ThreadActivity.EXTRA_ADDRESS, to)
                .putExtra(ThreadActivity.EXTRA_THREAD, to.isEmpty() ? 0 : SmsStore.threadIdFor(this, to));
        if (!body.isEmpty()) open.putExtra(Intent.EXTRA_TEXT, body);
        if (in != null && in.getData() != null) open.setData(in.getData());
        try { startActivity(open); } catch (Throwable ignored) { }
        finish();
    }
}
