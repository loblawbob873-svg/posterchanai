package place.poster.app.sms;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.provider.Telephony;
import android.telephony.SmsMessage;
import android.util.Log;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import place.poster.app.signer.SignerRelayService;

/**
 * WHERE A TEXT MESSAGE ARRIVES, and the one code path in this app that must never fail quietly.
 *
 * SMS_DELIVER goes to the DEFAULT messaging app only, and it is that app's job to write the message
 * into the system store. Nothing else does it. If this receiver drops a message, the message does
 * not exist: not in the phone's provider, not in any other app, not in a backup, not in the
 * conversation. There is no retry and nothing anywhere to say it happened.
 *
 * SO NOTHING HERE MAY THROW PAST THE TRY, and the steps are ordered by what matters: store, then
 * notify, then tell the app. Each is guarded separately — a notification that fails must not cost
 * the message, and the WebView never hearing about it must not cost the notification.
 *
 * A MULTIPART MESSAGE ARRIVES AS SEVERAL PDUs IN ONE BROADCAST. Concatenating them is the whole job;
 * getting it wrong stores a long text as its first 160 characters and discards the rest with no
 * error. Grouping is by SENDER, because two people can text in the same second and their parts
 * interleave in one delivery.
 *
 * It runs with the WebView dead, with the app never opened, and from a cold process. That is what a
 * broadcast receiver is for, and it is the reason the messages app here is native rather than a
 * screen inside a browser engine that Android is free to kill.
 */
public class SmsDeliverReceiver extends BroadcastReceiver {

    private static final String TAG = "PosterChan";

    @Override
    public void onReceive(Context ctx, Intent intent) {
        if (intent == null) return;
        if (!Telephony.Sms.Intents.SMS_DELIVER_ACTION.equals(intent.getAction())) return;
        try {
            deliver(ctx, intent);
        } catch (Throwable t) {
            Log.e(TAG, "sms: an incoming message was lost", t);
        }
    }

    private void deliver(Context ctx, Intent intent) {
        SmsMessage[] parts = Telephony.Sms.Intents.getMessagesFromIntent(intent);
        if (parts == null || parts.length == 0) return;

        Map<String, List<SmsMessage>> bySender = new LinkedHashMap<String, List<SmsMessage>>();
        for (SmsMessage p : parts) {
            if (p == null) continue;
            String from = p.getDisplayOriginatingAddress();
            if (from == null) from = p.getOriginatingAddress();
            if (from == null) from = "";
            List<SmsMessage> list = bySender.get(from);
            if (list == null) { list = new ArrayList<SmsMessage>(); bySender.put(from, list); }
            list.add(p);
        }

        for (Map.Entry<String, List<SmsMessage>> e : bySender.entrySet()) {
            String from = e.getKey();
            List<String> bodies = new ArrayList<String>();
            long when = 0, sentAt = 0;
            for (SmsMessage p : e.getValue()) {
                String b = null;
                try { b = p.getDisplayMessageBody(); } catch (Throwable ignored) { }
                if (b == null) try { b = p.getMessageBody(); } catch (Throwable ignored) { }
                bodies.add(b == null ? "" : b);
                try { if (p.getTimestampMillis() > when) when = p.getTimestampMillis(); } catch (Throwable ignored) { }
            }
            String body = SmsKeys.joinParts(bodies);
            // The SENDER's clock, not ours, EXCEPT when it is missing or absurd. A message filed
            // under 1970 sorts to the bottom of the thread for ever and reads as never arriving.
            long now = System.currentTimeMillis();
            if (when <= 0 || Math.abs(now - when) > 365L * 24 * 3600 * 1000) { sentAt = when; when = now; }

            Uri row = null;
            try {
                row = SmsStore.storeInbox(ctx, from, body, when, sentAt);
            } catch (Throwable t) {
                Log.e(TAG, "sms: could not store an incoming message", t);
            }

            try {
                SmsNotifier.incoming(ctx, from, body, when, SmsStore.threadIdFor(ctx, from));
            } catch (Throwable t) {
                Log.w(TAG, "sms: could not post a notification", t);
            }

            // LAST, and the only step whose failure is genuinely uninteresting: the app may not be
            // running at all. It is how an open Messages screen redraws without polling, and how the
            // Nostr archive learns there is something new to publish.
            try { SmsPlugin.onIncoming(from, body, when); } catch (Throwable ignored) { }
            // The WebView may be dead. The native signer already owns the account's relay socket,
            // so hand it the archive event and let every PosterChan client see the reply live.
            try { SignerRelayService.archiveIncoming(ctx, from, body, when); }
            catch (Throwable t) { Log.w(TAG, "sms: could not queue archive publish", t); }
        }
    }
}
