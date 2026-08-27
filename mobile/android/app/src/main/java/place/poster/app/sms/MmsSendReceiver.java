package place.poster.app.sms;

import android.content.BroadcastReceiver;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.database.Cursor;
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
                int http = intent.getIntExtra("android.telephony.extra.MMS_HTTP_STATUS", 0);
                int providerBox = 0;
                try (Cursor c = ctx.getContentResolver().query(row,
                        new String[]{Telephony.Mms.MESSAGE_BOX}, null, null, null)) {
                    if (c != null && c.moveToFirst()) providerBox = c.getInt(0);
                } catch (Throwable ignored) { }
                int status = MmsResult.classify(result, http, providerBox);
                boolean ok = status == MmsResult.SENT;
                boolean unknown = status == MmsResult.UNKNOWN;
                /* Code 0 is not a failure on several OEM carrier stacks: the same callback has
                 * been observed for delivered and undelivered MMS. Keep its provider row in the
                 * outbox and label it delivery-unknown. Marking it FAILED caused the UI to lie and
                 * encouraged a retry that could send the same photo repeatedly. */
                if (!unknown) {
                    ContentValues values = new ContentValues();
                    values.put(Telephony.Mms.MESSAGE_BOX, ok
                            ? Telephony.Mms.MESSAGE_BOX_SENT : Telephony.Mms.MESSAGE_BOX_FAILED);
                    ctx.getContentResolver().update(row, values, null, null);
                }
                long id = 0;
                try { id = Long.parseLong(row.getLastPathSegment()); } catch (Throwable ignored) { }
                if (ok) MmsFailures.clear(ctx, id); else MmsFailures.put(ctx, id, result, http);
                String file = intent.getStringExtra("file_path");
                if (file != null && !file.isEmpty()) new File(file).delete();
                /* An unknown result is deliberately not emitted as `ok:false`. The provider/error
                 * record is authoritative and the next thread reload displays its honest state. */
                if (!unknown) SmsPlugin.onSendResult(row.toString(), ok, result);
            } catch (Throwable t) {
                Log.w(TAG, "mms: could not finish send", t);
            } finally {
                MmsFlight.release(ctx);
                pending.finish();
            }
        }, "pc-mms-result").start();
    }
}
