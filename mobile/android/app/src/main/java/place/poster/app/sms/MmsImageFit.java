package place.poster.app.sms;

import java.util.Locale;

/**
 * FIT A PHOTO INSIDE THE MMS THE PLATFORM WILL ACTUALLY CARRY — and decide it here, Android-free,
 * so every rule below RUNS on a plain JVM (tests/test_android_mms_image_fit.py).
 *
 * WHY THIS EXISTS: the whole picture path was built on the belief, written into MmsLink and
 * MmsSender, that "mmslib resizes an image for the carrier". It does not. Decompiled
 * (org.fossify:mmslib:1.0.0, the same classes as Klinker's android-smsmms): `new Message(body, to,
 * Bitmap)` ends in `Message.bitmapToByteArray`, which is `image.compress(JPEG, 90, stream)` on the
 * FULL-RESOLUTION bitmap — no scaling anywhere — and `Transaction.sendMmsThroughSystem` then hands
 * the platform a PDU while overriding `maxMessageSize` with `MmsConfig.getMaxMessageSize()`
 * (819200). A 12-megapixel camera photo at quality 90 is megabytes. The platform's MmsService reads
 * at most maxMessageSize bytes of the PDU, returns null for anything bigger, and the request ends
 * with SmsManager.MMS_ERROR_IO_ERROR (5) — which is exactly the "Send status unconfirmed" /
 * MMS_ERROR_IO_ERROR every earlier fix chased as a SIM-subscription problem.
 *
 * The rule: the bytes that go into the PDU are produced HERE, as a JPEG no larger than the budget,
 * and handed to the library as media (`Message.addMedia`), so the library never re-encodes them.
 */
final class MmsImageFit {
    private MmsImageFit() { }

    /** PDU headers, the SMIL layout part, each part's headers, and the carrier's own slack. */
    static final int HEADROOM = 16 * 1024;
    /** Below this a "budget" is a misreport; a photo still has to be SOMETHING. */
    static final int MIN_BUDGET = 48 * 1024;
    /** Longest edge ladder. 1600 is already more than any MMS recipient's screen shows. */
    static final int[] EDGES = {1600, 1280, 1024, 800, 640, 480};
    /** Quality ladder tried at each edge before stepping the edge down. */
    static final int[] QUALITIES = {85, 72, 60, 48};

    /**
     * How many bytes of image may go into one MMS. The SMALLER of the carrier's published ceiling
     * and the transport's own cap (the library overrides the platform's maxMessageSize with its
     * value, so a carrier allowing more is still capped by it), minus the text and the headroom.
     */
    static int budget(int carrierLimit, int transportLimit, int bodyBytes) {
        int cap = carrierLimit > 0 ? carrierLimit : 300 * 1024;
        if (transportLimit > 0) cap = Math.min(cap, transportLimit);
        return Math.max(MIN_BUDGET, cap - HEADROOM - Math.max(0, bodyBytes));
    }

    /**
     * May these bytes go out UNTOUCHED? Only an animated-capable GIF that already fits: re-encoding
     * would flatten its animation. Everything else is re-encoded even when it fits, as it always
     * was — a camera JPEG carries EXIF (GPS position included) and a re-encode is what strips it.
     */
    static boolean sendAsIs(String mime, int bytes, int budget) {
        return "image/gif".equals(norm(mime)) && bytes > 0 && bytes <= budget;
    }

    /** Largest power-of-two decode subsample that still leaves the longest edge >= edge. */
    static int sampleSize(int width, int height, int edge) {
        int longest = Math.max(width, height);
        int s = 1;
        while (edge > 0 && longest / (s * 2) >= edge) s *= 2;
        return s;
    }

    /** Output size with the longest edge capped at `edge`, aspect kept, never upscaled. */
    static int[] scaled(int width, int height, int edge) {
        int longest = Math.max(width, height);
        if (width <= 0 || height <= 0) return new int[]{0, 0};
        if (longest <= edge) return new int[]{width, height};
        double f = edge / (double) longest;
        return new int[]{Math.max(1, (int) Math.round(width * f)),
                         Math.max(1, (int) Math.round(height * f))};
    }

    /** The one encoding step only a phone can perform. Null means "could not encode at all". */
    interface Encoder {
        byte[] encode(int edge, int quality) throws Exception;
    }

    /**
     * Walk the ladder, largest/best first, and return the first encoding within the budget. Null
     * when even the smallest rung does not fit — the caller REFUSES then, synchronously, with a
     * sentence; handing the platform an oversized PDU is how this failed silently before.
     */
    static byte[] fit(Encoder encoder, int width, int height, int budget) throws Exception {
        int longest = Math.max(width, height);
        int previousEdge = -1;
        for (int edge : EDGES) {
            int e = Math.min(edge, longest);
            if (e == previousEdge) continue;   // a small picture: every larger rung is the same size
            previousEdge = e;
            for (int q : QUALITIES) {
                byte[] out = encoder.encode(e, q);
                if (out != null && out.length > 0 && out.length <= budget) return out;
            }
        }
        return null;
    }

    static String tooLarge(int budget) {
        return "This picture could not be made small enough for your carrier's "
                + MmsAttachment.size(budget) + " picture-message limit.";
    }

    static String norm(String mime) {
        return mime == null ? "" : mime.split(";", 2)[0].trim().toLowerCase(Locale.ROOT);
    }
}
