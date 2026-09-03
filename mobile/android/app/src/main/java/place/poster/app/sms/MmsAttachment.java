package place.poster.app.sms;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.Locale;

/** Size-aware attachment staging shared by picker and send paths. */
final class MmsAttachment {
    static final int MAX_STAGED_BYTES = 8 * 1024 * 1024;

    static final class TooLarge extends IOException {
        final long size;
        final int limit;
        TooLarge(long size, int limit) {
            super("attachment exceeds MMS limit");
            this.size = size;
            this.limit = limit;
        }
    }

    private MmsAttachment() { }

    static byte[] read(InputStream in, int limit) throws IOException {
        if (in == null) throw new IOException("attachment cannot be read");
        ByteArrayOutputStream out = new ByteArrayOutputStream(Math.min(limit, 256 * 1024));
        byte[] buffer = new byte[64 * 1024];
        int total = 0;
        while (true) {
            int count = in.read(buffer);
            if (count < 0) break;
            if (count == 0) continue;
            if (total > limit - count) throw new TooLarge((long) total + count, limit);
            out.write(buffer, 0, count);
            total += count;
        }
        return out.toByteArray();
    }

    static void rejectKnownVideoSize(long size, int limit) throws TooLarge {
        if (size >= 0 && size > limit) throw new TooLarge(size, limit);
    }

    static String size(long bytes) {
        if (bytes < 0) return "unknown size";
        if (bytes < 1024) return bytes + " B";
        if (bytes < 1024 * 1024) return String.format(Locale.US, "%.0f KB", bytes / 1024.0);
        return String.format(Locale.US, "%.1f MB", bytes / (1024.0 * 1024.0));
    }

    static String tooLargeMessage(long size, int limit) {
        String actual = size < 0 ? "This video" : "This " + size(size) + " video";
        return actual + " is larger than your carrier's " + size(limit)
                + " MMS limit. Trim or compress it, then attach the smaller copy.";
    }
}
