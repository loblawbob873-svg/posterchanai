package place.poster.app.sms;

import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import android.app.UiAutomation;
import android.content.ContentResolver;
import android.content.ContentUris;
import android.content.ContentValues;
import android.content.Context;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.Paint;
import android.graphics.Rect;
import android.net.Uri;
import android.os.SystemClock;
import android.provider.Telephony;
import android.view.InputDevice;
import android.view.MotionEvent;

import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;

import org.junit.After;
import org.junit.Assume;
import org.junit.Before;
import org.junit.Test;
import org.junit.runner.RunWith;

import java.io.ByteArrayOutputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.List;

import place.poster.app.RealUi;
import place.poster.app.ui.ZoomImageView;

/**
 * "can't zoom in pictures in texts app" (2026-10-24): a picture in a conversation opened as a plain FIT_CENTER view
 * of the bubble's 900px thumbnail that closed on any tap. Here, on the native Messages screen, with a REAL incoming
 * picture message in the platform's MMS store: the photo is pressed with a real touch, and the viewer is driven with
 * touches injected through the system's input pipeline (UiAutomation) -- a double-tap, a two-finger pinch, a drag.
 * It must zoom, pan without leaving an empty band, not close while zoomed, close on a tap once back at fit, and show
 * the picture at more than the thumbnail's resolution.
 */
@RunWith(AndroidJUnit4.class)
public class MmsPictureZoomDeviceTest {
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
        RealUi.shell("input keyevent KEYCODE_BACK");
        ContentResolver cr = ctx.getContentResolver();
        for (Uri u : cleanup) try { cr.delete(u, null, null); } catch (Throwable ignored) { }
        cleanup.clear();
        if (previousDefault != null && !previousDefault.equals(Telephony.Sms.getDefaultSmsPackage(ctx))) {
            RealUi.shell("cmd role add-role-holder android.app.role.SMS " + previousDefault);
        }
    }

    @Test
    public void aPictureZoomsPansAndClosesOnlyWhenNotZoomed() throws Exception {
        Assume.assumeTrue("could not take the SMS role on this device", takeRole());
        String from = "+1555" + (1000000 + (int) (System.nanoTime() % 8000000));
        long thread = Telephony.Threads.getOrCreateThreadId(ctx, from);
        insertIncomingMms(from, thread, "image/jpeg", "pc-zoom-" + System.nanoTime() + ".jpg", bigJpeg());
        ctx.startActivity(new Intent(ctx, ThreadActivity.class)
                .putExtra(ThreadActivity.EXTRA_THREAD, thread)
                .putExtra(ThreadActivity.EXTRA_ADDRESS, from)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP));
        RealUi.tap("the photo in the conversation", RealUi.named("Photo"), 15_000);
        RealUi.Hit viewer = RealUi.waitFor("the picture viewer", RealUi.saying("double-tap to zoom"), 10_000);
        final ZoomImageView[] v = new ZoomImageView[1];
        RealUi.waitUntil("the viewer is on screen", 5_000, () -> (v[0] = ZoomImageView.last.get()) != null && v[0].isShown());

        // Sharp: the 2400x1600 original replaces the bubble's 900px thumbnail.
        RealUi.waitUntil("the viewer never showed more than the thumbnail (" + width(v[0]) + "px)", 10_000,
                () -> width(v[0]) > 900);

        Rect b = viewer.bounds;
        int cx = b.centerX(), cy = b.centerY();
        // Double-tap: zoom in around the finger.
        tap(cx, cy); SystemClock.sleep(90); tap(cx, cy);
        RealUi.waitUntil("a double-tap did not zoom in (zoom " + v[0].zoom() + ")", 3_000, () -> v[0].zoom() > 2f);

        // A single tap while zoomed must NOT close the picture.
        tap(cx, cy); SystemClock.sleep(600);
        assertTrue("a tap closed the picture while it was zoomed", v[0].isShown());

        // Drag: the picture moves, and never leaves an empty band at the edge it was dragged away from.
        // Every gesture stays inside the viewer: CI's screen is 320px wide, and an injected touch off it is dropped.
        int x0 = b.left + 20, x1 = b.right - 20, quarter = (x1 - x0) / 4;
        float before = left(v[0]);
        drag(cx - quarter, cy, cx + quarter, cy);
        RealUi.waitUntil("dragging a zoomed picture did not move it", 3_000, () -> Math.abs(left(v[0]) - before) > 20);
        for (int i = 0; i < 4; i++) drag(x0, cy, x1, cy);          // far past the picture's left edge
        SystemClock.sleep(300);
        assertTrue("the picture was dragged past its edge (left " + left(v[0]) + ")", left(v[0]) <= 1f);

        // Double-tap again: back to the whole picture; then a tap closes it.
        tap(cx, cy); SystemClock.sleep(90); tap(cx, cy);
        RealUi.waitUntil("a second double-tap did not return to fit (zoom " + v[0].zoom() + ")", 3_000,
                () -> v[0].zoom() < 1.05f);
        // Pinch out with two fingers: zooms past 1.5x.
        pinch(cx, cy, 15, (b.width() / 2) - 15);
        RealUi.waitUntil("a two-finger pinch did not zoom (zoom " + v[0].zoom() + ")", 3_000, () -> v[0].zoom() > 1.5f);
        tap(cx, cy); SystemClock.sleep(90); tap(cx, cy);
        RealUi.waitUntil("back to fit", 3_000, () -> v[0].zoom() < 1.05f);
        tap(cx, cy);
        RealUi.waitUntil("a tap at fit did not close the picture", 5_000, () -> !v[0].isShown());
    }

    // ------------------------------------------------------------------ injected touches (the system input pipeline)

    private static UiAutomation ua() { return InstrumentationRegistry.getInstrumentation().getUiAutomation(); }

    private static void inject(MotionEvent e) {
        e.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        assertTrue("the touch could not be injected", ua().injectInputEvent(e, true));
        e.recycle();
    }

    private static void tap(int x, int y) {
        long t = SystemClock.uptimeMillis();
        inject(MotionEvent.obtain(t, t, MotionEvent.ACTION_DOWN, x, y, 0));
        inject(MotionEvent.obtain(t, t + 40, MotionEvent.ACTION_UP, x, y, 0));
    }

    private static void drag(int x0, int y0, int x1, int y1) {
        long t = SystemClock.uptimeMillis();
        inject(MotionEvent.obtain(t, t, MotionEvent.ACTION_DOWN, x0, y0, 0));
        for (int i = 1; i <= 12; i++) {
            inject(MotionEvent.obtain(t, t + i * 16, MotionEvent.ACTION_MOVE, x0 + (x1 - x0) * i / 12f, y0 + (y1 - y0) * i / 12f, 0));
            SystemClock.sleep(16);
        }
        inject(MotionEvent.obtain(t, t + 13 * 16, MotionEvent.ACTION_UP, x1, y1, 0));
    }

    /** Two fingers moving apart horizontally from `from` to `to` px each side of the centre. */
    private static void pinch(int cx, int cy, int from, int to) {
        MotionEvent.PointerProperties[] pp = { new MotionEvent.PointerProperties(), new MotionEvent.PointerProperties() };
        for (int i = 0; i < 2; i++) { pp[i].id = i; pp[i].toolType = MotionEvent.TOOL_TYPE_FINGER; }
        long t = SystemClock.uptimeMillis();
        inject(MotionEvent.obtain(t, t, MotionEvent.ACTION_DOWN, 1, pp, coords(cx, cy, from, 1), 0, 0, 1, 1, 0, 0,
                InputDevice.SOURCE_TOUCHSCREEN, 0));
        inject(MotionEvent.obtain(t, t + 10, MotionEvent.ACTION_POINTER_DOWN | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT),
                2, pp, coords(cx, cy, from, 2), 0, 0, 1, 1, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0));
        for (int i = 1; i <= 15; i++) {
            int d = from + (to - from) * i / 15;
            inject(MotionEvent.obtain(t, t + 10 + i * 16, MotionEvent.ACTION_MOVE, 2, pp, coords(cx, cy, d, 2),
                    0, 0, 1, 1, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0));
            SystemClock.sleep(16);
        }
        inject(MotionEvent.obtain(t, t + 300, MotionEvent.ACTION_POINTER_UP | (1 << MotionEvent.ACTION_POINTER_INDEX_SHIFT),
                2, pp, coords(cx, cy, to, 2), 0, 0, 1, 1, 0, 0, InputDevice.SOURCE_TOUCHSCREEN, 0));
        inject(MotionEvent.obtain(t, t + 310, MotionEvent.ACTION_UP, 1, pp, coords(cx, cy, to, 1), 0, 0, 1, 1, 0, 0,
                InputDevice.SOURCE_TOUCHSCREEN, 0));
    }

    private static MotionEvent.PointerCoords[] coords(int cx, int cy, int d, int n) {
        MotionEvent.PointerCoords[] c = new MotionEvent.PointerCoords[n];
        for (int i = 0; i < n; i++) {
            c[i] = new MotionEvent.PointerCoords();
            c[i].x = i == 0 ? cx - d : cx + d; c[i].y = cy; c[i].pressure = 1; c[i].size = 1;
        }
        return c;
    }

    // ------------------------------------------------------------------ reading the viewer

    private static int width(ZoomImageView v) {
        Bitmap b = v == null ? null : v.bitmap();
        return b == null ? 0 : b.getWidth();
    }

    private static float left(ZoomImageView v) {
        float[] m = new float[9];
        v.getImageMatrix().getValues(m);
        return m[android.graphics.Matrix.MTRANS_X];
    }

    // ------------------------------------------------------------------ the platform's own MMS store (as ThreadSaveAttachmentTapDeviceTest)

    private boolean takeRole() {
        if (ctx.getPackageName().equals(Telephony.Sms.getDefaultSmsPackage(ctx))) return true;
        RealUi.shell("cmd role add-role-holder android.app.role.SMS " + ctx.getPackageName());
        return ctx.getPackageName().equals(Telephony.Sms.getDefaultSmsPackage(ctx));
    }

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
        v.put(Telephony.Mms.MESSAGE_TYPE, 132);
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

    /** 2400x1600 with fine detail: something worth zooming into, and well past the 900px thumbnail. */
    private static byte[] bigJpeg() {
        Bitmap b = Bitmap.createBitmap(2400, 1600, Bitmap.Config.ARGB_8888);
        Canvas c = new Canvas(b);
        c.drawColor(Color.rgb(30, 60, 120));
        Paint p = new Paint(); p.setColor(Color.WHITE); p.setStrokeWidth(2);
        for (int x = 0; x < 2400; x += 40) c.drawLine(x, 0, x, 1600, p);
        for (int y = 0; y < 1600; y += 40) c.drawLine(0, y, 2400, y, p);
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        b.compress(Bitmap.CompressFormat.JPEG, 85, out);
        return out.toByteArray();
    }
}
