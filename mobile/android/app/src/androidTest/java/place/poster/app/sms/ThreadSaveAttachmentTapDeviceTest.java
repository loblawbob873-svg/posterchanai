package place.poster.app.sms;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.content.ContentResolver;
import android.content.ContentUris;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.database.Cursor;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.net.Uri;
import android.provider.MediaStore;
import android.provider.Telephony;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.After;
import org.junit.Assume;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;

import place.poster.app.RealUi;
import place.poster.app.preview.MediaSaveRules;

/**
 * "sms apk does not let me save media attachments" (a0dde0052) -- PRESSED, on the native Messages
 * screen, for every kind of part a picture message carries.
 *
 * The off-device test runs MmsPartSave + MediaSaveWriter on a JVM against a recording MediaStore. What
 * it cannot see is whether the screen DRAWS a Save a person can press, whether the press reaches the
 * phone's real MMS part (content://mms/part/<id>, the platform provider, read across processes), and
 * whether the bytes really land in the phone's Downloads where a gallery or a file manager finds them.
 *
 * So each case puts a real incoming MMS into the platform's own store (as the default messages app,
 * which is the only app the provider lets write it -- the same role the app holds on a phone), opens
 * the conversation the way a notification does, finds "Save photo" / "Save video" / "Save audio" /
 * "Save <file name>" by the words on the button, presses it with a real touch, and reads the file back
 * out of MediaStore: under Download/PosterChan/<kind>/, byte for byte the part that was sent.
 */
@RunWith(AndroidJUnit4.class)
public class ThreadSaveAttachmentTapDeviceTest {

    private Context ctx;
    private String previousDefault;
    private final List<Uri> cleanup = new ArrayList<Uri>();

    @Before
    public void setUp() {
        ctx = InstrumentationRegistry.getInstrumentation().getTargetContext();
        previousDefault = Telephony.Sms.getDefaultSmsPackage(ctx);
        RealUi.awake();
    }

    @After
    public void tearDown() {
        RealUi.shell("input keyevent KEYCODE_BACK");
        ContentResolver cr = ctx.getContentResolver();
        for (Uri u : cleanup) try { cr.delete(u, null, null); } catch (Throwable ignored) { }
        cleanup.clear();
        if (previousDefault != null && !previousDefault.equals(Telephony.Sms.getDefaultSmsPackage(ctx))) {
            RealUi.shell("cmd role add-role-holder android.app.role.SMS " + previousDefault);
        }
    }

    @Test
    public void savePhotoLandsInDownloadsImages() throws Exception {
        savesThePart("image/jpeg", "pc-photo-" + System.nanoTime() + ".jpg", jpeg(), "Save photo", "Images/");
    }

    @Test
    public void saveVideoLandsInDownloadsVideos() throws Exception {
        savesThePart("video/mp4", "pc-video-" + System.nanoTime() + ".mp4", bytes(70_000, 7), "Save video", "Videos/");
    }

    @Test
    public void saveAudioLandsInDownloadsMusic() throws Exception {
        // AMR is what a phone's voice-note MMS actually carries.
        byte[] amr = bytes(9_000, 3);
        System.arraycopy("#!AMR\n".getBytes("US-ASCII"), 0, amr, 0, 6);
        savesThePart("audio/amr", "pc-voice-" + System.nanoTime() + ".amr", amr, "Save audio", "Music/");
    }

    @Test
    public void saveAnyOtherFileLandsInDownloadsFiles() throws Exception {
        String name = "pc-doc-" + System.nanoTime() + ".pdf";
        byte[] pdf = bytes(5_000, 11);
        System.arraycopy("%PDF-1.4\n".getBytes("US-ASCII"), 0, pdf, 0, 9);
        // A file the screen has no viewer for: the button is labelled with the sender's file name.
        savesThePart("application/pdf", name, pdf, "Save " + name, "Files/");
    }

    // ------------------------------------------------------------------ the case

    private void savesThePart(String ct, String name, byte[] data, String button, String sub) throws Exception {
        Assume.assumeTrue("could not take the SMS role on this device", takeRole());
        String from = "+1555" + (1000000 + (int) (System.nanoTime() % 8000000));
        long thread = Telephony.Threads.getOrCreateThreadId(ctx, from);
        insertIncomingMms(from, thread, ct, name, data);

        ctx.startActivity(new Intent(ctx, ThreadActivity.class)
                .putExtra(ThreadActivity.EXTRA_THREAD, thread)
                .putExtra(ThreadActivity.EXTRA_ADDRESS, from)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP));

        RealUi.tap("the \"" + button + "\" button under the attachment", RealUi.named(button), 15_000);

        final String folder = MediaSaveRules.ROOT + sub;
        final String file = MediaSaveRules.name(name, MediaSaveRules.mime(ct));
        final Uri[] saved = new Uri[1];
        RealUi.waitUntil("pressing \"" + button + "\" put no " + folder + file + " into the phone's Downloads",
                15_000, () -> (saved[0] = findDownload(folder, file)) != null);
        cleanup.add(saved[0]);
        byte[] back;
        try (InputStream in = ctx.getContentResolver().openInputStream(saved[0])) {
            assertNotNull("the saved file cannot be opened", in);
            back = readAll(in);
        }
        assertArrayEquals("the saved " + sub + " file is not the attachment that was sent", data, back);
    }

    // ------------------------------------------------------------------ the platform's own MMS store

    /** An incoming, downloaded picture message, written the way the platform's MMS stack files one:
     *  the pdu row, its FROM/TO addresses, and the part with its bytes streamed into the provider. */
    private void insertIncomingMms(String from, long thread, String ct, String name, byte[] data) throws Exception {
        ContentResolver cr = ctx.getContentResolver();
        long now = System.currentTimeMillis() / 1000;
        ContentValues v = new ContentValues();
        v.put(Telephony.Mms.THREAD_ID, thread);
        v.put(Telephony.Mms.DATE, now);
        v.put(Telephony.Mms.DATE_SENT, now);
        v.put(Telephony.Mms.MESSAGE_BOX, Telephony.Mms.MESSAGE_BOX_INBOX);
        v.put(Telephony.Mms.READ, 1);
        v.put(Telephony.Mms.SEEN, 1);
        v.put(Telephony.Mms.MESSAGE_TYPE, 132);            // m-retrieve-conf: downloaded, not a notification
        v.put(Telephony.Mms.CONTENT_TYPE, "application/vnd.wap.multipart.related");
        v.put(Telephony.Mms.MMS_VERSION, 0x12);
        Uri msg = cr.insert(Telephony.Mms.CONTENT_URI, v);
        assertNotNull("the MMS provider refused the message row", msg);
        cleanup.add(0, msg);
        long id = ContentUris.parseId(msg);

        for (Object[] a : new Object[][]{ { from, 137 }, { "insert-address-token", 151 } }) {
            ContentValues av = new ContentValues();
            av.put(Telephony.Mms.Addr.ADDRESS, (String) a[0]);
            av.put(Telephony.Mms.Addr.TYPE, (Integer) a[1]);
            av.put(Telephony.Mms.Addr.CHARSET, 106);
            cr.insert(Uri.parse("content://mms/" + id + "/addr"), av);
        }

        ContentValues pv = new ContentValues();
        pv.put(Telephony.Mms.Part.MSG_ID, id);
        pv.put(Telephony.Mms.Part.SEQ, 0);
        pv.put(Telephony.Mms.Part.CONTENT_TYPE, ct);
        pv.put(Telephony.Mms.Part.NAME, name);
        pv.put(Telephony.Mms.Part.CONTENT_LOCATION, name);
        pv.put(Telephony.Mms.Part.CONTENT_ID, "<" + name + ">");
        Uri part = cr.insert(Uri.parse("content://mms/" + id + "/part"), pv);
        assertNotNull("the MMS provider refused the part row", part);
        try (OutputStream out = cr.openOutputStream(part)) {
            assertNotNull("the MMS provider would not take the part's bytes", out);
            out.write(data);
        }
    }

    private Uri findDownload(String folder, String name) {
        Cursor c = null;
        try {
            c = ctx.getContentResolver().query(MediaStore.Downloads.EXTERNAL_CONTENT_URI,
                    new String[]{ MediaStore.MediaColumns._ID },
                    MediaStore.MediaColumns.RELATIVE_PATH + "=? AND " + MediaStore.MediaColumns.DISPLAY_NAME
                            + "=? AND " + MediaStore.MediaColumns.IS_PENDING + "=0",
                    new String[]{ folder, name }, null);
            if (c != null && c.moveToFirst()) {
                return ContentUris.withAppendedId(MediaStore.Downloads.EXTERNAL_CONTENT_URI, c.getLong(0));
            }
        } catch (Throwable ignored) {
        } finally {
            if (c != null) c.close();
        }
        return null;
    }

    private boolean takeRole() {
        if (ctx.getPackageName().equals(Telephony.Sms.getDefaultSmsPackage(ctx))) return true;
        RealUi.shell("cmd role add-role-holder android.app.role.SMS " + ctx.getPackageName());
        return ctx.getPackageName().equals(Telephony.Sms.getDefaultSmsPackage(ctx));
    }

    // ------------------------------------------------------------------ bytes

    private static byte[] jpeg() {
        Bitmap b = Bitmap.createBitmap(320, 200, Bitmap.Config.ARGB_8888);
        new Canvas(b).drawColor(Color.rgb(200, 40, 120));
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        b.compress(Bitmap.CompressFormat.JPEG, 80, out);
        return out.toByteArray();
    }

    private static byte[] bytes(int n, int seed) {
        byte[] b = new byte[n];
        for (int i = 0; i < n; i++) b[i] = (byte) ((i * 31 + seed) & 0xff);
        return b;
    }

    private static byte[] readAll(InputStream in) throws Exception {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buf = new byte[16384];
        int n;
        while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        return out.toByteArray();
    }
}
