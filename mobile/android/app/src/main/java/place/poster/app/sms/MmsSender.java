package place.poster.app.sms;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;

import com.klinker.android.send_message.Message;
import com.klinker.android.send_message.Settings;
import com.klinker.android.send_message.Transaction;

/** One carrier-MMS send path shared by the foreground plugin and background WebUI outbox. */
public final class MmsSender {
    private MmsSender() { }

    public static SmsSender.Result send(Context ctx, String to, String body, byte[] raw) {
        SmsSender.Result r = new SmsSender.Result();
        if (to == null || to.trim().isEmpty()) { r.error = "missing recipient"; return r; }
        if (raw == null || raw.length == 0) { r.error = "missing attachment"; return r; }
        if (raw.length > 8 * 1024 * 1024) { r.error = "picture message is too large"; return r; }
        try {
            BitmapFactory.Options bounds = new BitmapFactory.Options();
            bounds.inJustDecodeBounds = true;
            BitmapFactory.decodeByteArray(raw, 0, raw.length, bounds);
            if (bounds.outWidth <= 0 || bounds.outHeight <= 0
                    || (long) bounds.outWidth * (long) bounds.outHeight > 40_000_000L)
                throw new IllegalArgumentException("attachment image dimensions are unsafe");
            Bitmap image = BitmapFactory.decodeByteArray(raw, 0, raw.length);
            if (image == null) throw new IllegalArgumentException("attachment is not an image");
            Settings settings = new Settings();
            settings.setUseSystemSending(true);
            Message message = new Message(body == null ? "" : body, to, image);
            message.setSave(true);
            new Transaction(ctx, settings).sendNewMessage(message);
            r.ok = true;
            // Klinker's transaction is responsible for the provider copy when this app has role.
            r.stored = HasRole.sms(ctx);
        } catch (Throwable t) {
            r.error = t.getMessage() == null ? "could not send picture message" : t.getMessage();
        }
        return r;
    }
}
