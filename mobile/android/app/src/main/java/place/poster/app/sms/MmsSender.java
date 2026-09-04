package place.poster.app.sms;

import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.os.Bundle;
import android.telephony.SmsManager;
import android.telephony.SubscriptionManager;

import com.klinker.android.send_message.Message;
import com.klinker.android.send_message.Settings;
import com.klinker.android.send_message.Transaction;

/** One carrier-MMS send path shared by the foreground plugin and background WebUI outbox. */
public final class MmsSender {
    private MmsSender() { }

    public static SmsSender.Result send(Context ctx, String to, String body, byte[] raw) {
        return send(ctx, to, body, raw, "image/jpeg", "attachment.jpg");
    }

    public static SmsSender.Result send(Context ctx, String to, String body, byte[] raw,
                                        String mime, String name) {
        return send(ctx, to, body, raw, mime, name, null);
    }

    static SmsSender.Result send(Context ctx, String to, String body, byte[] raw,
                                 String mime, String name, String draftKey) {
        SmsSender.Result r = new SmsSender.Result();
        if (to == null || to.trim().isEmpty()) { r.error = "missing recipient"; return r; }
        if (raw == null || raw.length == 0) { r.error = "missing attachment"; return r; }
        String type = normalizedMime(mime, name);
        boolean video = type.startsWith("video/");
        int videoLimit = video ? videoLimit() : 0;
        // A video is too large for this carrier MMS when it exceeds this SIM's payload ceiling.
        if (video && raw.length > videoLimit) {
            r.error = MmsAttachment.tooLargeMessage(raw.length, videoLimit); return r;
        }
        // MAX_STAGED_BYTES is intentionally 8 * 1024 * 1024; keep transport and picker aligned.
        if (raw.length > MmsAttachment.MAX_STAGED_BYTES) {
            r.error = "That " + MmsAttachment.size(raw.length)
                    + " file is above the 8.0 MB attachment staging limit."; return r;
        }
        if (!MmsFlight.claim(ctx)) {
            r.error = "another picture message is still being sent";
            return r;
        }
        try {
            r.sentAt = System.currentTimeMillis();
            if (!video && !type.startsWith("image/"))
                throw new IllegalArgumentException("MMS supports photos and videos");
            /* Unlike photos, mmslib cannot resize/transcode a video. Refuse it synchronously above
             * the SIM's own ceiling instead of accepting a transaction the MMSC will silently drop.
             * BOTH COMPOSERS CHECK THE SAME LIMIT FIRST and turn the file into an encrypted link
             * instead — the Web one in sms.js:sendAsLink, the launcher's Texts app in
             * ThreadActivity.sendAsLink (MmsLink) — so this is the backstop for the retry and
             * background entry points, and reaching it means somebody sees the refusal above with
             * nothing to press. Do not make it the only answer again. */
            Settings settings = new Settings();
            settings.setUseSystemSending(true);
            /* MMS must leave through the subscription selected for messages. Relying on the
             * library's process-global default produces a valid provider outbox row but no carrier
             * transfer on dual-SIM phones (and on single-SIM devices whose default id is stale).
             * Prefer the explicit SMS subscription, then the active data subscription MMS uses. */
            int sub = SubscriptionManager.getDefaultSmsSubscriptionId();
            if (sub == SubscriptionManager.INVALID_SUBSCRIPTION_ID)
                sub = SubscriptionManager.getDefaultDataSubscriptionId();
            if (sub != SubscriptionManager.INVALID_SUBSCRIPTION_ID)
                settings.setSubscriptionId(sub);
            Message message;
            if (video) {
                message = new Message(body == null ? "" : body, to);
                message.addMedia(raw, type, name == null || name.isEmpty() ? "video" : name);
            } else {
                BitmapFactory.Options bounds = new BitmapFactory.Options();
                bounds.inJustDecodeBounds = true;
                BitmapFactory.decodeByteArray(raw, 0, raw.length, bounds);
                if (bounds.outWidth <= 0 || bounds.outHeight <= 0
                        || (long) bounds.outWidth * (long) bounds.outHeight > 40_000_000L)
                    throw new IllegalArgumentException("attachment image dimensions are unsafe");
                Bitmap image = BitmapFactory.decodeByteArray(raw, 0, raw.length);
                if (image == null) throw new IllegalArgumentException("attachment is not an image");
                message = new Message(body == null ? "" : body, to, image);
            }
            message.setSave(true);
            /* The library's default completion receiver is not contributed by its AAR manifest.
             * Without an explicit receiver Android accepts the send, but nobody moves the provider
             * row out of content://mms/outbox or removes the temporary PDU: the phone says
             * "Sending" forever. Route the carrier result to our declared receiver. */
            Intent sent = new Intent(ctx, MmsSendReceiver.class)
                    .setAction(MmsSendReceiver.ACTION_SENT);
            if (draftKey != null) sent.putExtra("draft_key", draftKey);
            new Transaction(ctx, settings).setExplicitBroadcastForSentMms(sent)
                    .sendNewMessage(message);
            r.ok = true;
            // Klinker's transaction is responsible for the provider copy when this app has role.
            r.stored = HasRole.sms(ctx);
        } catch (Throwable t) {
            MmsFlight.release(ctx);
            r.error = t.getMessage() == null ? "could not send picture message" : t.getMessage();
        }
        return r;
    }

    static int carrierLimit() {
        try {
            Bundle cfg = SmsManager.getDefault().getCarrierConfigValues();
            int bytes = cfg == null ? 0 : cfg.getInt("maxMessageSize", 0);
            if (bytes > 64 * 1024) return bytes;
        } catch (Throwable ignored) { }
        return 300 * 1024;
    }

    static int videoLimit() { return Math.max(64 * 1024, carrierLimit() - 8 * 1024); }

    static String normalizedMime(String mime, String name) {
        String type = mime == null ? "" : mime.split(";", 2)[0].trim()
                .toLowerCase(java.util.Locale.ROOT);
        if (!type.isEmpty() && !"application/octet-stream".equals(type)) return type;
        String n = name == null ? "" : name.toLowerCase(java.util.Locale.ROOT);
        if (n.endsWith(".mp4") || n.endsWith(".m4v")) return "video/mp4";
        if (n.endsWith(".mov")) return "video/quicktime";
        if (n.endsWith(".webm")) return "video/webm";
        if (n.endsWith(".3gp") || n.endsWith(".3gpp")) return "video/3gpp";
        if (n.endsWith(".jpg") || n.endsWith(".jpeg")) return "image/jpeg";
        if (n.endsWith(".png")) return "image/png";
        if (n.endsWith(".gif")) return "image/gif";
        if (n.endsWith(".webp")) return "image/webp";
        return type;
    }
}
