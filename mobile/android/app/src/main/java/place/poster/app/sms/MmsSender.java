package place.poster.app.sms;

import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Matrix;
import android.media.ExifInterface;
import android.os.Bundle;
import android.telephony.SmsManager;
import android.telephony.SubscriptionManager;

import com.klinker.android.send_message.Message;
import com.klinker.android.send_message.Settings;
import com.klinker.android.send_message.Transaction;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;

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
        return send(ctx, to == null ? new String[0] : new String[]{ to }, body, raw, mime, name, draftKey);
    }

    /**
     * A GROUP GETS ONE MESSAGE, NOT A PRIVATE COPY EACH. `Message(body, String[])` plus
     * `Settings.setGroup(true)` is what makes the platform send one MMS addressed to everybody, so
     * the replies stay in the conversation everyone can see. Addressed to one person it is exactly
     * the single-recipient send this method has always done.
     */
    static SmsSender.Result send(Context ctx, String[] to, String body, byte[] raw,
                                 String mime, String name, String draftKey) {
        SmsSender.Result r = new SmsSender.Result();
        if (to == null || to.length == 0 || to[0] == null || to[0].trim().isEmpty()) {
            r.error = "missing recipient"; return r;
        }
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
            // Several recipients = one group message. Without this the library sends each of them a
            // separate private message, which is the same wrong answer this screen used to give.
            settings.setGroup(to.length > 1);
            /* Route MMS over the ACTIVE subscription, resolved from getActiveSubscriptionInfoList() —
             * NOT a default-sub getter. On a phone whose physical SIM is disabled and only an eSIM is
             * active, getDefaultSmsSubscriptionId()/SmsManager.getDefault() can still hand back the
             * disabled slot's STALE id, and the carrier transaction then fails with MMS_ERROR_IO_ERROR
             * and the recipient never gets the picture. The single active subscription is unambiguously
             * the SIM that can actually send. With two active SIMs, prefer the user's messaging (SMS)
             * default, then the data subscription. When the list can't be read (READ_PHONE_STATE not
             * granted) leave the subscription unset and let the library default stand. */
            int sub = activeMmsSubscriptionId(ctx);
            if (sub != SubscriptionManager.INVALID_SUBSCRIPTION_ID)
                settings.setSubscriptionId(sub);
            Message message;
            if (video) {
                message = new Message(body == null ? "" : body, to);
                message.addMedia(raw, type, name == null || name.isEmpty() ? "video" : name);
            } else {
                /* THE PHOTO IS FITTED HERE, NOT BY THE LIBRARY — see MmsImageFit. The Bitmap
                 * constructor this used to call re-encodes the FULL-RESOLUTION picture at quality
                 * 90 and never scales it, so every camera photo became a multi-megabyte PDU that
                 * the platform refused with MMS_ERROR_IO_ERROR. The bytes handed over below are
                 * already within this SIM's ceiling and are passed as media, which the library
                 * copies into the PDU untouched. */
                int bodyBytes = body == null ? 0
                        : body.getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
                int budget = MmsImageFit.budget(carrierLimit(ctx), transportLimit(), bodyBytes);
                byte[] fitted = prepareImage(raw, type, budget);
                boolean asIs = fitted == raw;
                message = new Message(body == null ? "" : body, to);
                String partName = asIs ? safeName(name, "image.gif") : jpegName(name);
                message.addMedia(fitted, asIs ? type : "image/jpeg", partName, partName);
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

    /**
     * A TEXT MESSAGE TO A GROUP. SMS has no such thing: addressed to several people it is several
     * separate private messages, each starting its own conversation, and nobody in the group sees
     * anybody else's reply. So a group text is an MMS with no media and `setGroup(true)` — which is
     * what every other messaging app means by "group text".
     *
     * Single-recipient text stays ordinary SMS (SmsSender): it is cheaper, it needs no data
     * connection, and it is what a one-to-one conversation has always sent.
     */
    static SmsSender.Result sendGroupText(Context ctx, String[] to, String body) {
        SmsSender.Result r = new SmsSender.Result();
        if (to == null || to.length < 2) { r.error = "not a group"; return r; }
        if (body == null || body.trim().isEmpty()) { r.error = "nothing to send"; return r; }
        // Claimed and released on the SAME terms as a picture message: MmsSendReceiver releases the
        // flight on every carrier result, so sending without claiming would clear a picture's claim.
        if (!MmsFlight.claim(ctx)) { r.error = "another picture message is still being sent"; return r; }
        try {
            r.sentAt = System.currentTimeMillis();
            Settings settings = new Settings();
            settings.setUseSystemSending(true);
            settings.setGroup(true);
            int sub = activeMmsSubscriptionId(ctx);
            if (sub != SubscriptionManager.INVALID_SUBSCRIPTION_ID) settings.setSubscriptionId(sub);
            Message message = new Message(body, to);
            message.setSave(true);
            Intent sent = new Intent(ctx, MmsSendReceiver.class).setAction(MmsSendReceiver.ACTION_SENT);
            new Transaction(ctx, settings).setExplicitBroadcastForSentMms(sent).sendNewMessage(message);
            r.ok = true;
            r.stored = HasRole.sms(ctx);
        } catch (Throwable t) {
            MmsFlight.release(ctx);
            r.error = t.getMessage() == null ? "could not send group message" : t.getMessage();
        }
        return r;
    }

    /** The subscription id MMS should leave on. Exactly one active SIM → that SIM's real id (never a
     * stale default from a disabled slot — the eSIM-only case). Two or more → the messaging (SMS)
     * default, then the data subscription. Cannot read the active list (no READ_PHONE_STATE) → INVALID,
     * so the caller leaves the library default in place. */
    static int activeMmsSubscriptionId(Context ctx) {
        try {
            SubscriptionManager sm = (SubscriptionManager)
                    ctx.getSystemService(Context.TELEPHONY_SUBSCRIPTION_SERVICE);
            java.util.List<android.telephony.SubscriptionInfo> active =
                    sm == null ? null : sm.getActiveSubscriptionInfoList();
            if (active != null && active.size() == 1) {
                return active.get(0).getSubscriptionId();
            }
            if (active != null && active.size() >= 2) {
                int sub = SubscriptionManager.getDefaultSmsSubscriptionId();
                if (sub == SubscriptionManager.INVALID_SUBSCRIPTION_ID)
                    sub = SubscriptionManager.getDefaultDataSubscriptionId();
                return sub;
            }
        } catch (Throwable t) {
            // fall through to INVALID
        }
        return SubscriptionManager.INVALID_SUBSCRIPTION_ID;
    }

    /** The MMS ceiling for the default SMS manager (callers with no Context), transport-capped. */
    static int carrierLimit() {
        SmsManager manager = null;
        try { manager = SmsManager.getDefault(); } catch (Throwable ignored) { }
        return ceiling(carrierConfigLimit(manager));
    }

    /** The same, read from the subscription this send actually leaves on. */
    static int carrierLimit(Context ctx) {
        SmsManager manager = null;
        try {
            int sub = activeMmsSubscriptionId(ctx);
            if (sub != SubscriptionManager.INVALID_SUBSCRIPTION_ID)
                manager = SmsManager.getSmsManagerForSubscriptionId(sub);
        } catch (Throwable ignored) { }
        if (manager == null) try { manager = SmsManager.getDefault(); } catch (Throwable ignored) { }
        return ceiling(carrierConfigLimit(manager));
    }

    private static int carrierConfigLimit(SmsManager manager) {
        try {
            Bundle cfg = manager == null ? null : manager.getCarrierConfigValues();
            int bytes = cfg == null ? 0 : cfg.getInt("maxMessageSize", 0);
            if (bytes > 64 * 1024) return bytes;
        } catch (Throwable ignored) { }
        return 300 * 1024;
    }

    /**
     * THE TRANSPORT'S OWN CAP. mmslib passes `maxMessageSize = MmsConfig.getMaxMessageSize()`
     * (819200 unless its config XML says otherwise) to SmsManager.sendMultimediaMessage as a config
     * OVERRIDE, so the platform refuses any PDU above it with MMS_ERROR_IO_ERROR even on a carrier
     * that publishes a larger ceiling. Every size decision — the photo budget, the video refusal,
     * the WebView's link threshold (SmsPlugin.mmsLimit) — must sit under BOTH numbers.
     */
    static int transportLimit() {
        try {
            int v = com.android.mms.MmsConfig.getMaxMessageSize();
            if (v > 64 * 1024) return v;
        } catch (Throwable ignored) { }
        return 800 * 1024;
    }

    /** The lower of a carrier figure and the transport cap. */
    static int ceiling(int carrier) { return Math.min(carrier, transportLimit()); }

    static int videoLimit() { return Math.max(64 * 1024, carrierLimit() - 8 * 1024); }

    /**
     * The bytes a photo goes into the PDU as: at most `budget`, JPEG, EXIF orientation applied (a
     * re-encode drops the EXIF tag that told the recipient how to rotate it — and with it the GPS
     * position, which is why an ordinary photo is re-encoded even when it already fits),
     * transparency flattened onto white. A GIF that already fits goes untouched so its animation
     * survives. Throws with a sentence when the picture cannot be read or cannot be made small
     * enough — never hands the platform a PDU it will refuse.
     */
    static byte[] prepareImage(final byte[] raw, String type, int budget) throws Exception {
        if (MmsImageFit.sendAsIs(type, raw.length, budget)) return raw;
        BitmapFactory.Options bounds = new BitmapFactory.Options();
        bounds.inJustDecodeBounds = true;
        BitmapFactory.decodeByteArray(raw, 0, raw.length, bounds);
        if (bounds.outWidth <= 0 || bounds.outHeight <= 0)
            throw new IllegalArgumentException("attachment is not an image");
        if ((long) bounds.outWidth * (long) bounds.outHeight > 40_000_000L)
            throw new IllegalArgumentException("attachment image dimensions are unsafe");
        final int rotation = exifRotation(raw);
        final int w = bounds.outWidth, h = bounds.outHeight;
        // One decode per rung of the edge ladder, reused for every quality tried at that edge.
        final Bitmap[] held = new Bitmap[1];
        final int[] heldEdge = {-1};
        try {
            byte[] out = MmsImageFit.fit((edge, quality) -> {
                if (heldEdge[0] != edge) {
                    if (held[0] != null) held[0].recycle();
                    held[0] = scaledBitmap(raw, w, h, edge, rotation);
                    heldEdge[0] = edge;
                }
                if (held[0] == null) throw new IllegalArgumentException("attachment is not an image");
                ByteArrayOutputStream stream = new ByteArrayOutputStream();
                held[0].compress(Bitmap.CompressFormat.JPEG, quality, stream);
                return stream.toByteArray();
            }, w, h, budget);
            if (out == null) throw new IllegalArgumentException(MmsImageFit.tooLarge(budget));
            return out;
        } finally {
            if (held[0] != null) held[0].recycle();
        }
    }

    private static Bitmap scaledBitmap(byte[] raw, int w, int h, int edge, int rotation) {
        BitmapFactory.Options o = new BitmapFactory.Options();
        o.inSampleSize = MmsImageFit.sampleSize(w, h, edge);
        Bitmap decoded = BitmapFactory.decodeByteArray(raw, 0, raw.length, o);
        if (decoded == null) return null;
        int[] size = MmsImageFit.scaled(decoded.getWidth(), decoded.getHeight(), edge);
        Matrix m = new Matrix();
        m.postScale(size[0] / (float) decoded.getWidth(), size[1] / (float) decoded.getHeight());
        if (rotation != 0) m.postRotate(rotation);
        Bitmap shaped = Bitmap.createBitmap(decoded, 0, 0, decoded.getWidth(), decoded.getHeight(), m, true);
        if (shaped != decoded) decoded.recycle();
        if (!shaped.hasAlpha()) return shaped;
        // JPEG has no alpha channel: a transparent PNG/WebP would otherwise arrive on BLACK.
        Bitmap flat = Bitmap.createBitmap(shaped.getWidth(), shaped.getHeight(), Bitmap.Config.ARGB_8888);
        Canvas c = new Canvas(flat);
        c.drawColor(Color.WHITE);
        c.drawBitmap(shaped, 0, 0, null);
        shaped.recycle();
        return flat;
    }

    private static int exifRotation(byte[] raw) {
        try {
            ExifInterface exif = new ExifInterface(new ByteArrayInputStream(raw));
            switch (exif.getAttributeInt(ExifInterface.TAG_ORIENTATION,
                    ExifInterface.ORIENTATION_NORMAL)) {
                case ExifInterface.ORIENTATION_ROTATE_90: return 90;
                case ExifInterface.ORIENTATION_ROTATE_180: return 180;
                case ExifInterface.ORIENTATION_ROTATE_270: return 270;
                default: return 0;
            }
        } catch (Throwable ignored) {
            return 0;
        }
    }

    /** A re-encoded picture is a JPEG whatever it was called, and its PDU name says so. */
    static String jpegName(String name) {
        String n = safeName(name, "image.jpg");
        int dot = n.lastIndexOf('.');
        return (dot > 0 ? n.substring(0, dot) : n) + ".jpg";
    }

    /** PDU content-location/content-id come from this: plain ASCII, never a path or a dotfile. */
    static String safeName(String name, String fallback) {
        String n = name == null ? "" : name.replaceAll("[^A-Za-z0-9._-]", "_");
        return n.isEmpty() || n.startsWith(".") ? fallback : n;
    }

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
