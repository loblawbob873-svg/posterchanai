package place.poster.app.sms;

import android.content.Context;
import android.net.Uri;
import android.util.Log;

import com.klinker.android.send_message.MmsReceivedReceiver;

/** Finishes a carrier MMS download, then wakes every consumer of the phone message store. */
public class MmsDownloadedReceiver extends MmsReceivedReceiver {
    private static final String TAG = "PosterChan";

    @Override
    public void onMessageReceived(Context ctx, Uri uri) {
        try {
            SmsMsg m = MmsStore.one(ctx, uri);
            if (m == null) throw new IllegalStateException("downloaded MMS row cannot be read");
            String body = m.body == null || m.body.trim().isEmpty() ? "Picture message" : m.body;
            SmsNotifier.incoming(ctx, m.address, body, m.date, m.threadId);
            SmsPlugin.onIncoming(m.address, body, m.date);
        } catch (Throwable t) {
            Log.e(TAG, "mms: downloaded message could not be announced", t);
            SmsNotifier.mmsError(ctx, "Picture message downloaded but could not be opened");
        }
    }

    @Override
    public void onError(Context ctx, String error) {
        Log.e(TAG, "mms: carrier download failed: " + error);
        SmsNotifier.mmsError(ctx, "Picture message could not be downloaded");
    }
}
