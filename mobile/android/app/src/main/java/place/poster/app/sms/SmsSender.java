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
        /** Whether the phone's OWN message store has a copy. False when we sent without the role. */
        public boolean stored;
    }

    /**
     * Send one text. Returns as soon as the radio has been asked — delivery is asynchronous and lands
     * in SmsActionReceiver, which is what moves the row from outbox to sent or failed.
     */
    public static Result send(Context ctx, String address, String body) {
        return send(ctx, address, body, 0);
    }

    /**
     * With the conversation's own thread id, so the reply lands IN it. See SmsStore.storeSent: asking
     * the platform to resolve the address instead can mint a second thread for the same person.
     */
    public static Result send(Context ctx, String address, String body, long threadId) {
        Result r = new Result();
        if (address == null || address.trim().isEmpty()) { r.error = "no number"; return r; }
        if (body == null || body.isEmpty()) { r.error = "nothing to send"; return r; }
        /* SENDING DOES NOT NEED THE ROLE. WRITING THE PROVIDER DOES.
         *
         * This used to refuse outright, and the reasoning was sound as far as it went: a non-default
         * app may call SmsManager with SEND_SMS but may not write the message store, so the sent
         * message would be missing from the thread it was sent in — "it didn't send".
         *
         * It does not hold HERE, because this screen does not render the provider. It renders our
         * own encrypted archive, which we can always write; the copy that would be missing is the
         * one in the phone's STOCK messages app. So the trade was: refuse to send at all, on every
         * phone that has not granted the role, to avoid a gap in a different app's UI. Reported as
         * "POsterchan is not the this phones messaging app when i send message" — a texting app that
         * cannot text.
         *
         * So it sends either way, and says which happened. `stored` false means the radio was asked
         * and the phone's own store has no copy; the caller puts it in the archive and says so once,
         * rather than pretending nothing was sent. */
        boolean mayWrite = HasRole.sms(ctx);

        long now = System.currentTimeMillis();
        r.row = mayWrite
                ? SmsStore.storeSent(ctx, address, body, now, Telephony.Sms.MESSAGE_TYPE_OUTBOX,
                                     threadId)
                : null;
        r.stored = r.row != null;

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
            // Only if there is a row to move. Without the role there is none, and marking a null one
            // failed is how a send that legitimately went out gets reported as broken.
            if (r.row != null) SmsStore.setType(ctx, r.row, Telephony.Sms.MESSAGE_TYPE_FAILED);
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
