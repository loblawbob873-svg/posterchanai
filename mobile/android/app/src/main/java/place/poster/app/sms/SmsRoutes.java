package place.poster.app.sms;

import android.content.Context;
import android.content.Intent;

/** One in-app route into a cellular conversation, shared by Phone and the WebView Contacts card. */
public final class SmsRoutes {
    private SmsRoutes() { }

    /** A direct child activity intent, or null when there is no usable recipient. Never NEW_TASK. */
    public static Intent conversation(Context ctx, String raw) {
        String number = SmsKeys.normalize(raw);
        if (ctx == null || number.isEmpty()) return null;
        return new Intent(ctx, ThreadActivity.class)
                .putExtra(ThreadActivity.EXTRA_ADDRESS, number)
                .putExtra(ThreadActivity.EXTRA_THREAD, SmsStore.threadIdFor(ctx, number))
                .addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
    }
}
