package place.poster.app.sms;

import android.content.Context;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.BitmapShader;
import android.graphics.Canvas;
import android.graphics.ColorFilter;
import android.graphics.Matrix;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.Rect;
import android.graphics.Shader;
import android.graphics.drawable.Drawable;
import android.net.Uri;
import android.provider.ContactsContract;

import java.io.InputStream;

import java.util.HashMap;
import java.util.Map;

/**
 * WHO IS THIS NUMBER — asked of the phone's whole address book, on purpose.
 *
 * PosterChan already syncs an address book into ContactsContract (place.poster.app.contacts), and
 * every query in that package is SCOPED to our own account type: it has to be, because it is a
 * reconcile and a short keep-set there is a delete order. This is the opposite question. Caller ID
 * and a message thread must name whoever the person has in their phone — a work account, a Google
 * account, a card typed into the Contacts app — so this queries PhoneLookup across ALL accounts and
 * lives in its own file rather than muddying that invariant.
 *
 * NO SECOND CONTACT STORE. Nothing here writes, and nothing here keeps a list; the phone's provider
 * is the only address book, and PosterChan's contacts reach it the way they already do.
 *
 * The cache is per-process and small: a thread list resolves the same twenty numbers on every draw,
 * and a PhoneLookup is a cross-process query each time.
 */
public final class PhoneBook {

    private static final Map<String, String> NAMES = new HashMap<String, String>();
    private static final Map<String, String> PHOTOS = new HashMap<String, String>();
    private static final Map<String, Bitmap> PHOTO_BYTES = new HashMap<String, Bitmap>();
    private static final int MAX = 512;

    private PhoneBook() { }

    /** The contact's display name, or "" when the number is not in the address book. */
    public static String nameOf(Context ctx, String number) {
        if (ctx == null || number == null || number.isEmpty()) return "";
        String key = SmsKeys.normalize(number);
        if (key.isEmpty()) return "";
        synchronized (NAMES) {
            String hit = NAMES.get(key);
            if (hit != null) return hit;
        }
        String name = lookup(ctx, number, ContactsContract.PhoneLookup.DISPLAY_NAME);
        synchronized (NAMES) {
            if (NAMES.size() > MAX) NAMES.clear();     // a cap, not an LRU: this is a hint, not state
            NAMES.put(key, name);
        }
        return name;
    }

    /** The contact's photo URI, or "" — used for the avatar beside a thread. */
    public static String photoOf(Context ctx, String number) {
        if (ctx == null || number == null || number.isEmpty()) return "";
        String key = SmsKeys.normalize(number);
        if (key.isEmpty()) return "";
        synchronized (PHOTOS) {
            String hit = PHOTOS.get(key);
            if (hit != null) return hit;
        }
        String uri = lookup(ctx, number, ContactsContract.PhoneLookup.PHOTO_THUMBNAIL_URI);
        synchronized (PHOTOS) {
            if (PHOTOS.size() > MAX) PHOTOS.clear();
            PHOTOS.put(key, uri);
        }
        return uri;
    }

    /** A fresh circular drawable for the contact photo, or null for the initials fallback. */
    public static Drawable photoDrawable(Context ctx, String number) {
        String uri = photoOf(ctx, number);
        if (uri.isEmpty()) return null;
        Bitmap bitmap;
        synchronized (PHOTO_BYTES) { bitmap = PHOTO_BYTES.get(uri); }
        if (bitmap == null) {
            InputStream in = null;
            try {
                in = ctx.getContentResolver().openInputStream(Uri.parse(uri));
                bitmap = BitmapFactory.decodeStream(in);
                if (bitmap != null) synchronized (PHOTO_BYTES) {
                    if (PHOTO_BYTES.size() > MAX) PHOTO_BYTES.clear();
                    PHOTO_BYTES.put(uri, bitmap);
                }
            } catch (Throwable ignored) {
                return null;
            } finally {
                if (in != null) try { in.close(); } catch (Throwable ignored) { }
            }
        }
        if (bitmap == null) return null;
        return new CircleBitmapDrawable(bitmap);
    }

    /** Framework-only so caller photos work in the app, device tests and minimal Android builds. */
    private static final class CircleBitmapDrawable extends Drawable {
        private final Bitmap bitmap;
        private final BitmapShader shader;
        private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG | Paint.FILTER_BITMAP_FLAG);
        private final Matrix matrix = new Matrix();

        CircleBitmapDrawable(Bitmap source) {
            bitmap = source;
            shader = new BitmapShader(source, Shader.TileMode.CLAMP, Shader.TileMode.CLAMP);
            paint.setShader(shader);
        }

        @Override protected void onBoundsChange(Rect bounds) {
            float scale = Math.max(bounds.width() / (float) Math.max(1, bitmap.getWidth()),
                                   bounds.height() / (float) Math.max(1, bitmap.getHeight()));
            float dx = bounds.left + (bounds.width() - bitmap.getWidth() * scale) / 2f;
            float dy = bounds.top + (bounds.height() - bitmap.getHeight() * scale) / 2f;
            matrix.reset(); matrix.setScale(scale, scale); matrix.postTranslate(dx, dy);
            shader.setLocalMatrix(matrix);
        }

        @Override public void draw(Canvas canvas) {
            Rect b = getBounds();
            canvas.drawCircle(b.exactCenterX(), b.exactCenterY(),
                              Math.min(b.width(), b.height()) / 2f, paint);
        }
        @Override public void setAlpha(int alpha) { paint.setAlpha(alpha); invalidateSelf(); }
        @Override public void setColorFilter(ColorFilter filter) { paint.setColorFilter(filter); invalidateSelf(); }
        @Override public int getOpacity() { return PixelFormat.TRANSLUCENT; }
        @Override public int getIntrinsicWidth() { return bitmap.getWidth(); }
        @Override public int getIntrinsicHeight() { return bitmap.getHeight(); }
    }

    private static String lookup(Context ctx, String number, String column) {
        Cursor c = null;
        try {
            Uri uri = Uri.withAppendedPath(ContactsContract.PhoneLookup.CONTENT_FILTER_URI,
                                           Uri.encode(number));
            c = ctx.getContentResolver().query(uri, new String[]{ column }, null, null, null);
            if (c != null && c.moveToFirst()) {
                String v = c.getString(0);
                return v == null ? "" : v;
            }
        } catch (Throwable ignored) {
            // No READ_CONTACTS, a locked device, a provider that refused. A number with no name is a
            // number with no name — never an error on a screen showing somebody's messages.
        } finally {
            if (c != null) try { c.close(); } catch (Throwable ignored) { }
        }
        return "";
    }

    /** What to show for a number: the contact's name if we know it, otherwise the number itself. */
    public static String label(Context ctx, String number) {
        String n = nameOf(ctx, number);
        return n.isEmpty() ? (number == null ? "" : number) : n;
    }

    /**
     * Forget everything. Called when the address book changes — a contact saved while a thread is on
     * screen must show its new name, and this cache would otherwise hold the number for the life of
     * the process.
     */
    public static void forget() {
        synchronized (NAMES) { NAMES.clear(); }
        synchronized (PHOTOS) { PHOTOS.clear(); }
        synchronized (PHOTO_BYTES) { PHOTO_BYTES.clear(); }
    }
}
