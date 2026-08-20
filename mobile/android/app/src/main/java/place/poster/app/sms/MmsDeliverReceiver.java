package place.poster.app.sms;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

/**
 * PICTURE MESSAGES ARRIVE HERE, AND THIS APP CANNOT YET FETCH THEM. IT SAYS SO.
 *
 * Android will not let an app hold the SMS role at all unless it declares a WAP_PUSH_DELIVER
 * receiver, so this class has to exist. What it does NOT do is pretend.
 *
 * Retrieving an MMS is a different piece of work from receiving a text: the broadcast carries an
 * M-Notification.ind PDU (WSP-encoded), the content lives on the carrier's MMSC and has to be
 * fetched over the MMS APN via SmsManager.downloadMultimediaMessage, and the M-Retrieve.conf that
 * comes back has to be decomposed into the pdu/addr/part tables of the MMS provider. That is several
 * hundred lines of binary parsing that cannot be exercised without a SIM and a carrier, on the one
 * code path where a mistake means somebody's message is gone. Writing a placeholder row into the
 * provider would be worse than nothing — it would put a message that does not exist into every app
 * and every backup on the phone.
 *
 * So: a NOTIFICATION, saying plainly that a picture message arrived and that PosterChan cannot fetch
 * it. The person can switch their messages app back and receive it. The opt-in screen says the same
 * thing BEFORE the role is taken, which is the only honest place to say it.
 *
 * Nothing about the provider is touched. An MMS not fetched is an MMS still waiting at the carrier.
 */
public class MmsDeliverReceiver extends BroadcastReceiver {

    private static final String TAG = "PosterChan";

    @Override
    public void onReceive(Context ctx, Intent intent) {
        if (intent == null) return;
        try {
            Log.i(TAG, "sms: a picture message arrived; PosterChan does not fetch MMS");
            SmsNotifier.mmsUnsupported(ctx);
        } catch (Throwable t) {
            Log.w(TAG, "sms: could not report an unfetched picture message", t);
        }
    }
}
