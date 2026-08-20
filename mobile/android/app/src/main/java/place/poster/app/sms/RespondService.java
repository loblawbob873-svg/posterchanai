package place.poster.app.sms;

import android.app.Service;
import android.content.Intent;
import android.os.IBinder;
import android.util.Log;

/**
 * "REPLY WITH A MESSAGE" FROM THE INCOMING-CALL SCREEN.
 *
 * The fourth thing Android requires before an app may hold the SMS role, and the least visible: when
 * somebody rejects a call with a canned text, the dialer hands it to the default messaging app
 * through ACTION_RESPOND_VIA_MESSAGE. Without this service the app cannot be chosen as the default
 * at all — and with a service that ignores the intent, the reply is silently never sent, which is
 * worse, because the person watched the phone tell them it had been.
 *
 * The URI carries the recipient in its scheme-specific part (`sms:+15550100`, `smsto:`, `mms:`,
 * `mmsto:`), and the text arrives as an extra. Both are read defensively: this is started by another
 * app's UI and the shape varies between OEM dialers.
 */
public class RespondService extends Service {

    private static final String TAG = "PosterChan";
    /** The platform's own extra name for the canned text. */
    private static final String EXTRA_TEXT = "android.intent.extra.TEXT";

    @Override public IBinder onBind(Intent intent) { return null; }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        try {
            String to = SendTo.numberFrom(intent == null ? null : intent.getData());
            CharSequence body = intent == null ? null : intent.getCharSequenceExtra(EXTRA_TEXT);
            if (body == null && intent != null) body = intent.getStringExtra(Intent.EXTRA_TEXT);
            if (!to.isEmpty() && body != null && body.length() > 0) {
                SmsSender.send(this, to, body.toString());
            } else {
                Log.w(TAG, "sms: respond-via-message had no number or no text");
            }
        } catch (Throwable t) {
            Log.w(TAG, "sms: respond-via-message failed", t);
        }
        // NOT sticky: this is one message, not a session. START_STICKY would have Android restart
        // the service with a null intent after a kill and re-run this method with nothing in it.
        stopSelf(startId);
        return START_NOT_STICKY;
    }
}
