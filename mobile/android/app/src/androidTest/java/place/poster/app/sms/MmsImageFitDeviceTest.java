package place.poster.app.sms;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertSame;
import static org.junit.Assert.assertTrue;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.BitmapFactory;
import android.graphics.Color;
import android.media.ExifInterface;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import com.google.android.mms.MMSPart;
import com.klinker.android.send_message.Message;
import com.klinker.android.send_message.Settings;
import com.klinker.android.send_message.Transaction;

import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.nio.file.Files;
import java.util.Random;

/**
 * A CAMERA PHOTO FITS IN THE MMS THE PLATFORM WILL CARRY — measured with Android's own decoder and
 * JPEG encoder, and with the mmslib PDU composer the send path really uses.
 *
 * The pre-fix sender handed mmslib a full-resolution Bitmap, which it compressed at quality 90 with
 * no scaling; the platform's MmsService refuses any PDU above the `maxMessageSize` the library passes
 * it (MmsSender.transportLimit) with MMS_ERROR_IO_ERROR. No carrier is contacted here — nothing is
 * sent — only the bytes that WOULD be handed to SmsManager are built and measured.
 */
@RunWith(AndroidJUnit4.class)
public class MmsImageFitDeviceTest {
    private static final int KB = 1024;

    /** A camera-sized frame with sensor-like noise, so JPEG cannot cheat on a flat colour. */
    private static Bitmap cameraFrame(int w, int h) {
        Bitmap b = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
        Random r = new Random(7);
        int[] row = new int[w];
        for (int y = 0; y < h; y++) {
            for (int x = 0; x < w; x++) {
                int n = r.nextInt(25) - 12;
                int edge = ((x / 97) + (y / 61)) % 2 == 0 ? 30 : 0;
                row[x] = Color.rgb(clamp(x * 255 / w + n + edge), clamp(y * 255 / h + n),
                        clamp(128 + n - edge));
            }
            b.setPixels(row, 0, w, 0, y, w, 1);
        }
        return b;
    }

    private static int clamp(int v) { return Math.max(0, Math.min(255, v)); }

    private static byte[] jpeg(Bitmap b, int q) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        b.compress(Bitmap.CompressFormat.JPEG, q, out);
        return out.toByteArray();
    }

    private static int[] bounds(byte[] raw) {
        BitmapFactory.Options o = new BitmapFactory.Options();
        o.inJustDecodeBounds = true;
        BitmapFactory.decodeByteArray(raw, 0, raw.length, o);
        return new int[]{o.outWidth, o.outHeight};
    }

    @Test
    public void thePreFixEncodingOfACameraPhotoIsOverTheTransportCap() {
        Bitmap cam = cameraFrame(4000, 3000);
        try {
            // Exactly what `new Message(body, to, bitmap)` put into the PDU before this fix.
            int size = Message.bitmapToByteArray(cam).length;
            assertTrue("full-resolution q90 was " + size + " bytes; the transport cap is "
                    + MmsSender.transportLimit(), size > MmsSender.transportLimit());
        } finally { cam.recycle(); }
    }

    @Test
    public void aCameraPhotoIsFittedUnderA300KbCarrierAndItsRealPduFits() throws Exception {
        Context ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        Bitmap cam = cameraFrame(4000, 3000);
        byte[] raw;
        try { raw = jpeg(cam, 95); } finally { cam.recycle(); }
        String body = "look at this";
        int budget = MmsImageFit.budget(300 * KB, MmsSender.transportLimit(), body.length());
        byte[] fitted = MmsSender.prepareImage(raw, "image/jpeg", budget);
        assertTrue("fitted " + fitted.length + " > budget " + budget, fitted.length <= budget);
        int[] wh = bounds(fitted);
        assertTrue(Math.max(wh[0], wh[1]) <= 1600);
        assertEquals("aspect kept", wh[0] * 3, wh[1] * 4);

        // The PDU mmslib would compose from these parts, measured with its own composer.
        new Transaction(ctx, new Settings());
        MMSPart image = new MMSPart();
        image.name = "photo.jpg"; image.fileName = "photo.jpg"; image.mimeType = "image/jpeg";
        image.data = fitted;
        MMSPart text = new MMSPart();
        text.name = "text"; text.mimeType = "text/plain"; text.data = body.getBytes("UTF-8");
        Transaction.MessageInfo info = Transaction.getBytes(ctx, false, "",
                new String[]{"+15550100"}, new MMSPart[]{image, text}, null);
        assertNotNull(info.bytes);
        assertTrue("PDU " + info.bytes.length + " over 300 KB", info.bytes.length <= 300 * KB);
        assertTrue(info.bytes.length <= MmsSender.transportLimit());
    }

    @Test
    public void exifOrientationIsAppliedBecauseTheReencodeDropsTheTag() throws Exception {
        Context ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        Bitmap wide = cameraFrame(400, 200);
        File f = new File(ctx.getCacheDir(), "mms-fit-exif.jpg");
        try {
            try (FileOutputStream out = new FileOutputStream(f)) { out.write(jpeg(wide, 90)); }
            ExifInterface exif = new ExifInterface(f.getAbsolutePath());
            exif.setAttribute(ExifInterface.TAG_ORIENTATION,
                    String.valueOf(ExifInterface.ORIENTATION_ROTATE_90));
            exif.saveAttributes();
            byte[] raw = Files.readAllBytes(f.toPath());
            byte[] fitted = MmsSender.prepareImage(raw, "image/jpeg", 500 * KB);
            int[] wh = bounds(fitted);
            assertEquals("rotated to portrait", 200, wh[0]);
            assertEquals(400, wh[1]);
        } finally { wide.recycle(); f.delete(); }
    }

    @Test
    public void aTransparentPngArrivesOnWhiteNotBlack() throws Exception {
        Bitmap clear = Bitmap.createBitmap(64, 64, Bitmap.Config.ARGB_8888);
        clear.eraseColor(Color.TRANSPARENT);
        ByteArrayOutputStream png = new ByteArrayOutputStream();
        clear.compress(Bitmap.CompressFormat.PNG, 100, png);
        clear.recycle();
        byte[] fitted = MmsSender.prepareImage(png.toByteArray(), "image/png", 300 * KB);
        Bitmap back = BitmapFactory.decodeByteArray(fitted, 0, fitted.length);
        try {
            int px = back.getPixel(32, 32);
            assertTrue("transparent became " + Integer.toHexString(px),
                    Color.red(px) > 240 && Color.green(px) > 240 && Color.blue(px) > 240);
        } finally { back.recycle(); }
    }

    @Test
    public void aGifThatFitsIsSentByteForByte() throws Exception {
        // The smallest valid GIF: 1x1, one colour. Re-encoding would flatten an animation.
        byte[] gif = new byte[]{0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 1, 0, 1, 0, (byte) 0x80, 0, 0,
                0, 0, 0, (byte) 0xff, (byte) 0xff, (byte) 0xff, 0x21, (byte) 0xf9, 4, 1, 0, 0, 0, 0,
                0x2c, 0, 0, 0, 0, 1, 0, 1, 0, 0, 2, 2, 0x44, 1, 0, 0x3b};
        assertSame(gif, MmsSender.prepareImage(gif, "image/gif", 300 * KB));
    }
}
