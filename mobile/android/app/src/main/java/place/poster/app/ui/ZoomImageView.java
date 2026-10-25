package place.poster.app.ui;

import android.content.Context;
import android.graphics.Bitmap;
import android.graphics.Matrix;
import android.graphics.RectF;
import android.graphics.drawable.Drawable;
import android.view.GestureDetector;
import android.view.MotionEvent;
import android.view.ScaleGestureDetector;
import android.widget.ImageView;

import java.lang.ref.WeakReference;

/**
 * A picture you can ZOOM: pinch (1x..6x), double-tap (2.5x around the finger, and back), drag while zoomed, the
 * picture always held to the screen's edges. A single tap -- confirmed, so it is never the first half of a
 * double-tap -- calls {@link #setOnTap} only while the picture is at its fitted size, so panning a zoomed picture
 * never closes it.
 *
 * Texts used a plain FIT_CENTER ImageView whose every tap closed it ("can't zoom in pictures in texts app",
 * 2026-10-24). MmsPictureZoomDeviceTest drives this with injected touches on the emulator.
 */
public class ZoomImageView extends ImageView {
    public static final float MAX = 6f, DOUBLE_TAP = 2.5f;

    /** The viewer most recently shown, for the device test to read its zoom and bitmap. */
    public static WeakReference<ZoomImageView> last = new WeakReference<>(null);

    private final Matrix matrix = new Matrix();
    private final float[] values = new float[9];
    private float fitted = 1f;
    private Runnable onTap;
    private final ScaleGestureDetector pinch;
    private final GestureDetector gestures;

    public ZoomImageView(Context context) {
        super(context);
        setScaleType(ScaleType.MATRIX);
        last = new WeakReference<>(this);
        pinch = new ScaleGestureDetector(context, new ScaleGestureDetector.SimpleOnScaleGestureListener() {
            @Override public boolean onScale(ScaleGestureDetector d) {
                zoomBy(d.getScaleFactor(), d.getFocusX(), d.getFocusY());
                return true;
            }
        });
        gestures = new GestureDetector(context, new GestureDetector.SimpleOnGestureListener() {
            @Override public boolean onDown(MotionEvent e) { return true; }
            @Override public boolean onSingleTapConfirmed(MotionEvent e) {
                if (!isZoomed() && onTap != null) onTap.run();
                return true;
            }
            @Override public boolean onDoubleTap(MotionEvent e) {
                if (isZoomed()) fit();
                else zoomBy(DOUBLE_TAP, e.getX(), e.getY());
                return true;
            }
            @Override public boolean onScroll(MotionEvent a, MotionEvent b, float dx, float dy) {
                if (!isZoomed() || pinch.isInProgress()) return false;
                matrix.postTranslate(-dx, -dy);
                keepOnScreen();
                return true;
            }
        });
    }

    /** Called on a single tap while the picture is at its fitted size (e.g. to close the viewer). */
    public void setOnTap(Runnable r) { onTap = r; }

    /** How far the picture is zoomed past its fitted size (1 = fitted). */
    public float zoom() {
        matrix.getValues(values);
        return fitted > 0 ? values[Matrix.MSCALE_X] / fitted : 1f;
    }

    public boolean isZoomed() { return zoom() > 1.01f; }

    /** The bitmap on show (the device test checks the sharp copy replaced the thumbnail). */
    public Bitmap bitmap() {
        Drawable d = getDrawable();
        return d instanceof android.graphics.drawable.BitmapDrawable ? ((android.graphics.drawable.BitmapDrawable) d).getBitmap() : null;
    }

    /**
     * Swap in a sharper copy of the SAME picture without moving it: the zoom and the point at the middle of the
     * screen are kept, so a person already zoomed in sees it sharpen rather than jump.
     */
    public void replaceBitmap(Bitmap sharper) {
        Drawable old = getDrawable();
        if (old == null || sharper == null || getWidth() == 0) { setImageBitmap(sharper); fit(); return; }
        float z = zoom();
        float[] mid = { getWidth() / 2f, getHeight() / 2f };
        Matrix inverse = new Matrix();
        matrix.invert(inverse);
        inverse.mapPoints(mid);
        float nx = mid[0] / Math.max(1, old.getIntrinsicWidth()), ny = mid[1] / Math.max(1, old.getIntrinsicHeight());
        setImageBitmap(sharper);
        fit();
        if (z > 1.01f) {
            matrix.postScale(z, z, getWidth() / 2f, getHeight() / 2f);
            float[] p = { nx * sharper.getWidth(), ny * sharper.getHeight() };
            matrix.mapPoints(p);
            matrix.postTranslate(getWidth() / 2f - p[0], getHeight() / 2f - p[1]);
            keepOnScreen();
        }
    }

    @Override protected void onSizeChanged(int w, int h, int ow, int oh) {
        super.onSizeChanged(w, h, ow, oh);
        fit();
    }

    @Override public boolean onTouchEvent(MotionEvent e) {
        pinch.onTouchEvent(e);
        gestures.onTouchEvent(e);
        if (getParent() != null) getParent().requestDisallowInterceptTouchEvent(isZoomed());
        return true;
    }

    /** Back to the whole picture, centred. */
    public void fit() {
        Drawable d = getDrawable();
        if (d == null || getWidth() == 0 || getHeight() == 0) return;
        float dw = Math.max(1, d.getIntrinsicWidth()), dh = Math.max(1, d.getIntrinsicHeight());
        fitted = Math.min(getWidth() / dw, getHeight() / dh);
        matrix.setScale(fitted, fitted);
        matrix.postTranslate((getWidth() - dw * fitted) / 2f, (getHeight() - dh * fitted) / 2f);
        setImageMatrix(matrix);
    }

    private void zoomBy(float factor, float fx, float fy) {
        float z = zoom(), next = Math.max(1f, Math.min(MAX, z * factor));
        if (next == z) return;
        float f = next / z;
        matrix.postScale(f, f, fx, fy);
        if (next <= 1.0001f) { fit(); return; }
        keepOnScreen();
    }

    /** No empty band beside a picture wider than the screen; centred along an axis where it is narrower. */
    private void keepOnScreen() {
        Drawable d = getDrawable();
        if (d == null) return;
        RectF r = new RectF(0, 0, d.getIntrinsicWidth(), d.getIntrinsicHeight());
        matrix.mapRect(r);
        float dx = 0, dy = 0, w = getWidth(), h = getHeight();
        if (r.width() <= w) dx = (w - r.width()) / 2f - r.left;
        else if (r.left > 0) dx = -r.left;
        else if (r.right < w) dx = w - r.right;
        if (r.height() <= h) dy = (h - r.height()) / 2f - r.top;
        else if (r.top > 0) dy = -r.top;
        else if (r.bottom < h) dy = h - r.bottom;
        matrix.postTranslate(dx, dy);
        setImageMatrix(matrix);
    }
}
