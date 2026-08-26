package place.poster.app.sms;

import android.app.Activity;
import android.content.BroadcastReceiver;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.provider.Telephony;
import android.util.Log;

import java.io.File;

/** Finishes a carrier MMS transaction in the phone's authoritative message provider. */
public final class MmsSendReceiver extends BroadcastReceiver {
    public static final String ACTION_SENT = "place.poster.app.MMS_SENT";
    private static final String TAG = "PosterChan";

    @Override public void onReceive(Context ctx, Intent intent) {
        if (intent == null || !ACTION_SENT.equals(intent.getAction())) return;
        final PendingResult pending = goAsync();
        final int result = getResultCode();
        new Thread(() -> {
            try {
                String value = intent.getStringExtra("content_uri");
                if (value == null || value.isEmpty()) throw new Exception("missing MMS provider row");
                Uri row = Uri.parse(value);
                /* Some carrier/OEM MMS services return Activity.RESULT_CANCELED (0) after they
                 * accepted and delivered the PDU. Treating that as FAILED exposed Retry and sent
                 * the same pictures repeatedly. MMS's documented transport failures are positive
                 * codes; zero therefore means accepted with no delivery confirmation here. */
                boolean ok = result == Activity.RESULT_OK || result == Activity.RESULT_CANCELED;
                ContentValues values = new ContentValues();
                values.put(Telephony.Mms.MESSAGE_BOX, ok
                        ? Telephony.Mms.MESSAGE_BOX_SENT : Telephony.Mms.MESSAGE_BOX_FAILED);
                ctx.getContentResolver().update(row, values, null, null);
                long id = 0;
                try { id = Long.parseLong(row.getLastPathSegment()); } catch (Throwable ignored) { }
                int http = intent.getIntExtra("android.telephony.extra.MMS_HTTP_STATUS", 0);
                if (ok) MmsFailures.clear(ctx, id); else MmsFailures.put(ctx, id, result, http);
                String file = intent.getStringExtra("file_path");
                if (file != null && !file.isEmpty()) new File(file).delete();
                SmsPlugin.onSendResult(row.toString(), ok, result);
            } catch (Throwable t) {
                Log.w(TAG, "mms: could not finish send", t);
            } finally {
                pending.finish();
            }
        }, "pc-mms-result").start();
    }
}
