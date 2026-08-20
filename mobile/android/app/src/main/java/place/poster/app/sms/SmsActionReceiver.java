package place.poster.app.sms;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Bundle;
import android.provider.Telephony;
import android.util.Log;

import androidx.core.app.RemoteInput;

/**
 * The four things that happen to a message outside a screen: a reply typed into the shade, a
 * conversation marked read from the shade, and the radio answering that a send worked or did not.
 *
 * All four are broadcasts, and all four have to work with no activity on screen and the WebView
 * dead — which is the whole reason the messages app here is native.
 */
public class SmsActionReceiver extends BroadcastReceiver {

    private static final String TAG = "PosterChan";

    public static final String ACTION_REPLY = "place.poster.app.SMS_REPLY";
    public static final String ACTION_MARK_READ = "place.poster.app.SMS_MARK_READ";
    public static final String KEY_REPLY = "pc_sms_reply";
    public static final String EXTRA_ADDRESS = "address";
    public static final String EXTRA_THREAD = "thread";

    @Override
    public void onReceive(Context ctx, Intent intent) {
        if (intent == null || intent.getAction() == null) return;
        String action = intent.getAction();
        long threadId = intent.getLongExtra(EXTRA_THREAD, 0);
        String address = intent.getStringExtra(EXTRA_ADDRESS);
        try {
            if (ACTION_REPLY.equals(action)) {
                Bundle in = RemoteInput.getResultsFromIntent(intent);
                CharSequence text = in == null ? null : in.getCharSequence(KEY_REPLY);
                if (address != null && text != null && text.length() > 0) {
                    SmsSender.send(ctx, address, text.toString());
                    SmsStore.markRead(ctx, threadId);
                    SmsNotifier.clear(ctx, threadId);
                } else {
                    // An empty reply means the RemoteInput did not come through — the classic cause
                    // is an IMMUTABLE PendingIntent (see SmsNotifier.replyAction). Say so rather than
                    // sending nothing and taking the notification down as if it had worked.
                    Log.w(TAG, "sms: a shade reply arrived with no text");
                }
                return;
            }
            if (ACTION_MARK_READ.equals(action)) {
                SmsStore.markRead(ctx, threadId);
                SmsNotifier.clear(ctx, threadId);
                return;
            }
            if (SmsSender.ACTION_SENT.equals(action) || SmsSender.ACTION_DELIVERED.equals(action)) {
                String row = intent.getStringExtra(SmsSender.EXTRA_ROW);
                if (row == null) return;                 // an intermediate part — see SmsSender.send
                boolean ok = getResultCode() == android.app.Activity.RESULT_OK;
                if (SmsSender.ACTION_SENT.equals(action)) {
                    SmsStore.setType(ctx, Uri.parse(row), ok
                            ? Telephony.Sms.MESSAGE_TYPE_SENT
                            : Telephony.Sms.MESSAGE_TYPE_FAILED);
                    SmsPlugin.onSendResult(row, ok, getResultCode());
                }
                return;
            }
        } catch (Throwable t) {
            Log.w(TAG, "sms: " + action + " failed", t);
        }
    }
}
