package place.poster.app.sms;

import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.provider.Telephony;
import android.telephony.SmsManager;
import android.util.Log;

import java.util.ArrayList;

/**
 * SENDING A TEXT, AND RECORDING THAT WE DID.
 *
 * Two halves, and the second is the one that is easy to forget: the radio call, and the row in the
 * phone's own message store. A default SMS app that sends without storing leaves the conversation
 * looking one-sided in every other app on the phone and in every backup — and in its own thread the
 * next time it reads the provider instead of its own memory.
 *
 * So the row is written FIRST, as OUTBOX, before the radio is asked. If the process dies mid-send
 * the message is visible as pending rather than absent; the alternative loses what somebody typed.
 * The sent/failed transition comes back through SmsActionReceiver.
 */
public final class SmsSender {

    private static final String TAG = "PosterChan";

    public static final String ACTION_SENT = "place.poster.app.SMS_SENT";
    public static final String ACTION_DELIVERED = "place.poster.app.SMS_DELIVERED";
    public static final String EXTRA_ROW = "row";

    private SmsSender() { }

    public static final class Result {
        public boolean ok;
        public String error = "";
        public Uri row;
        public int parts;
    }

    /**
     * Send one text. Returns as soon as the radio has been asked — delivery is asynchronous and lands
     * in SmsActionReceiver, which is what moves the row from outbox to sent or failed.
     */
    public static Result send(Context ctx, String address, String body) {
        Result r = new Result();
        if (address == null || address.trim().isEmpty()) { r.error = "no number"; return r; }
        if (body == null || body.isEmpty()) { r.error = "nothing to send"; return r; }
        if (!HasRole.sms(ctx)) {
            // Not a technicality. A non-default app CAN still call SmsManager with SEND_SMS, but it
            // may not write the provider — so the message would be sent and then be missing from the
            // thread it was sent in, which reads as "it didn't send".
            r.error = "PosterChan is not this phone's messages app";
            return r;
        }

        long now = System.currentTimeMillis();
        r.row = SmsStore.storeSent(ctx, address, body, now, Telephony.Sms.MESSAGE_TYPE_OUTBOX);

        try {
            SmsManager sms = manager(ctx);
            ArrayList<String> parts = sms.divideMessage(body);
            r.parts = parts.size();
            ArrayList<PendingIntent> sent = new ArrayList<PendingIntent>();
            ArrayList<PendingIntent> delivered = new ArrayList<PendingIntent>();
            for (int i = 0; i < parts.size(); i++) {
                // ONE PendingIntent PER PART, and only the LAST one carries the row: a three-part
                // message answers three times, and treating each answer as "the message is sent"
                // moves the row to sent while two parts are still in the air.
                boolean last = i == parts.size() - 1;
                sent.add(signal(ctx, ACTION_SENT, last ? r.row : null, i));
                delivered.add(signal(ctx, ACTION_DELIVERED, last ? r.row : null, i));
            }
            sms.sendMultipartTextMessage(address, null, parts, sent, delivered);
            r.ok = true;
        } catch (Throwable t) {
            Log.w(TAG, "sms: send failed", t);
            r.error = String.valueOf(t.getMessage());
            SmsStore.setType(ctx, r.row, Telephony.Sms.MESSAGE_TYPE_FAILED);
        }
        return r;
    }

    private static SmsManager manager(Context ctx) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
            SmsManager m = ctx.getSystemService(SmsManager.class);
            if (m != null) return m;
        }
        return SmsManager.getDefault();
    }

    private static PendingIntent signal(Context ctx, String action, Uri row, int part) {
        Intent i = new Intent(ctx, SmsActionReceiver.class).setAction(action);
        if (row != null) i.putExtra(EXTRA_ROW, row.toString());
        // MUTABLE would let anything holding this intent rewrite the row it points at. The platform
        // fills the result code in through the PendingIntent's own result, not through extras, so
        // immutable is not merely allowed here — it is correct.
        int flags = PendingIntent.FLAG_UPDATE_CURRENT
                | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0);
        return PendingIntent.getBroadcast(ctx, (action + part + row).hashCode(), i, flags);
    }
}
