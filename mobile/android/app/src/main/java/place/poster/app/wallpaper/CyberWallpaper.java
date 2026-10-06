package place.poster.app.wallpaper;

import android.app.WallpaperInfo;
import android.app.WallpaperManager;
import android.content.BroadcastReceiver;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.graphics.Bitmap;
import android.graphics.BitmapShader;
import android.graphics.BitmapFactory;
import android.graphics.Canvas;
import android.graphics.ColorMatrix;
import android.graphics.ColorMatrixColorFilter;
import android.graphics.LinearGradient;
import android.graphics.Paint;
import android.graphics.RadialGradient;
import android.graphics.RectF;
import android.graphics.Shader;
import android.graphics.Typeface;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.service.wallpaper.WallpaperService;
import android.util.Log;
import android.view.SurfaceHolder;

/**
 * The PosterChan cyberpunk live wallpaper — {@link CyberScene} on a phone's home screen.
 *
 * BATTERY IS THE DESIGN, NOT A DETAIL. A wallpaper lives as long as the phone is on, and the home screen
 * it sits behind has one standing rule: nothing ticks while nobody is looking. So:
 *   * frames are scheduled ONLY while Android reports the wallpaper visible ({@link FramePolicy}); the
 *     moment it is hidden — screen off, an app in front — the pending frame is removed and nothing is
 *     scheduled until it is visible again. No timer, no wake lock, no alarm, ever;
 *   * everything that does not move (sky, moon, every building and window) is drawn ONCE per surface size
 *     into two bitmaps and blitted; a frame is a few dozen primitives on top;
 *   * battery saver drops it to ~12 fps rather than freezing it (a frozen animated wallpaper reads as
 *     broken), and that is re-read the moment the mode changes, not at the next start.
 *
 * Taps on the empty home screen arrive as {@link WallpaperManager#COMMAND_TAP} — the launcher forwards
 * them (DeskView does) — and make PosterChan hop and ripple the wet roof. Page swipes move the skyline (parallax).
 */
public class CyberWallpaper extends WallpaperService {

    private static final String TAG = "CyberWallpaper";

    @Override
    public Engine onCreateEngine() {
        return new CyberEngine();
    }

    // ------------------------------------------------------------------ setting it, from the launcher

    /** The intent that opens Android's own preview-and-apply screen ON this wallpaper. */
    public static Intent previewIntent(Context ctx) {
        Intent i = new Intent(WallpaperManager.ACTION_CHANGE_LIVE_WALLPAPER);
        i.putExtra(WallpaperManager.EXTRA_LIVE_WALLPAPER_COMPONENT, new ComponentName(ctx, CyberWallpaper.class));
        return i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
    }

    /**
     * Open the preview screen, or — on a build that has none — the live-wallpaper picker, where this one
     * is listed. False when neither exists, so the caller can SAY so: a menu item that does nothing is
     * indistinguishable from one that is not wired up.
     */
    public static boolean open(Context ctx) {
        try { ctx.startActivity(previewIntent(ctx)); return true; } catch (Throwable t) { Log.w(TAG, "no live wallpaper preview", t); }
        try {
            ctx.startActivity(new Intent(WallpaperManager.ACTION_LIVE_WALLPAPER_CHOOSER).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
            return true;
        } catch (Throwable t) { Log.w(TAG, "no live wallpaper chooser", t); }
        return false;
    }

    /** Is this the wallpaper on the home screen right now? */
    public static boolean isActive(Context ctx) {
        try {
            WallpaperInfo info = WallpaperManager.getInstance(ctx).getWallpaperInfo();
            return info != null && ctx.getPackageName().equals(info.getPackageName())
                    && CyberWallpaper.class.getName().equals(info.getServiceName());
        } catch (Throwable t) {
            return false;
        }
    }

    // ------------------------------------------------------------------------------------- engine

    class CyberEngine extends Engine {
        private final Handler handler = new Handler(Looper.getMainLooper());
        private final Runnable frame = new Runnable() { @Override public void run() { draw(); } };
        private final long started = SystemClock.uptimeMillis();
        private boolean visible = false;
        private boolean powerSave = false;
        private float offset = 0.5f;
        private int width, height;
        private CyberScene scene;
        private Bitmap sky, city;
        private float cityTop;
        private final float[] taps = new float[12];   // up to 4 pulses: {x, y, tStart}
        private int tapNext = 0;
        private CanvasPen pen;
        private Bitmap[] dance;

        private final BroadcastReceiver saver = new BroadcastReceiver() {
            @Override public void onReceive(Context c, Intent i) {
                readPowerSave();
                if (visible) { handler.removeCallbacks(frame); draw(); }
            }
        };

        @Override
        public void onCreate(SurfaceHolder holder) {
            super.onCreate(holder);
            setOffsetNotificationsEnabled(true);
            readPowerSave();
            dance = loadDance();
            try {
                registerReceiver(saver, new IntentFilter(PowerManager.ACTION_POWER_SAVE_MODE_CHANGED));
            } catch (Throwable ignored) { }
        }

        @Override
        public void onDestroy() {
            handler.removeCallbacks(frame);
            try { unregisterReceiver(saver); } catch (Throwable ignored) { }
            release();
            if (dance != null) for (Bitmap b : dance) if (b != null) b.recycle();
            dance = null;
            super.onDestroy();
        }

        @Override
        public void onVisibilityChanged(boolean v) {
            visible = v;
            handler.removeCallbacks(frame);
            if (v) draw();            // draws now and, through FramePolicy, schedules the next
        }

        @Override
        public void onSurfaceChanged(SurfaceHolder holder, int format, int w, int h) {
            super.onSurfaceChanged(holder, format, w, h);
            if (w != width || h != height || scene == null) build(w, h);
            draw();
        }

        @Override
        public void onSurfaceDestroyed(SurfaceHolder holder) {
            visible = false;
            handler.removeCallbacks(frame);
            super.onSurfaceDestroyed(holder);
        }

        @Override
        public void onOffsetsChanged(float xOffset, float yOffset, float xStep, float yStep, int xPx, int yPx) {
            offset = Float.isNaN(xOffset) ? 0.5f : xOffset;
            // No draw here: a visible wallpaper is already drawing, and an invisible one must not.
        }

        @Override
        public Bundle onCommand(String action, int x, int y, int z, Bundle extras, boolean resultRequested) {
            if (WallpaperManager.COMMAND_TAP.equals(action)) {
                int i = (tapNext++ % 4) * 3;
                taps[i] = x; taps[i + 1] = y; taps[i + 2] = now();
            }
            return null;
        }

        /**
         * PosterChan's dance, decoded ONCE for the engine's life (eight 406x560 frames, ~7 MB). Looked up by
         * name rather than through R so this class type-checks off-device; a frame that will not decode is
         * simply skipped (CanvasPen.sprite draws nothing for it), never a crash on the home screen.
         */
        private Bitmap[] loadDance() {
            Bitmap[] out = new Bitmap[CyberScene.FRAMES];
            try {
                BitmapFactory.Options o = new BitmapFactory.Options();
                o.inScaled = false;
                for (int i = 0; i < out.length; i++) {
                    int id = getResources().getIdentifier("pc_dance_" + (i + 1), "drawable", getPackageName());
                    if (id != 0) out[i] = BitmapFactory.decodeResource(getResources(), id, o);
                }
            } catch (Throwable t) {
                Log.w(TAG, "could not load the dance frames", t);
            }
            return out;
        }

        private float now() { return (SystemClock.uptimeMillis() - started) / 1000f; }

        private void readPowerSave() {
            try {
                PowerManager pm = (PowerManager) getSystemService(Context.POWER_SERVICE);
                powerSave = pm != null && pm.isPowerSaveMode();
            } catch (Throwable t) { powerSave = false; }
        }

        /** The static layers, rendered once per surface size. */
        private void build(int w, int h) {
            release();
            width = w; height = h;
            if (w <= 0 || h <= 0) return;
            try {
                scene = new CyberScene(w, h, 0x50C4L);
                sky = Bitmap.createBitmap(w, h, Bitmap.Config.ARGB_8888);
                scene.drawSky(new CanvasPen(new Canvas(sky)));
                cityTop = scene.skylineTop();
                int cw = (int) Math.ceil(CyberScene.SPAN * w);
                int ch = (int) Math.ceil(scene.horizon - cityTop) + 2;
                city = Bitmap.createBitmap(cw, Math.max(1, ch), Bitmap.Config.ARGB_8888);
                scene.drawSkyline(new CanvasPen(new Canvas(city)), 0, -cityTop);
            } catch (Throwable t) {
                // Out of memory on a huge surface: fall back to drawing the static layers every frame
                // rather than taking the home screen's wallpaper down with it.
                Log.w(TAG, "could not cache the static layers", t);
                if (sky != null) { sky.recycle(); sky = null; }
                if (city != null) { city.recycle(); city = null; }
            }
        }

        private void release() {
            if (sky != null) { sky.recycle(); sky = null; }
            if (city != null) { city.recycle(); city = null; }
        }

        private void draw() {
            SurfaceHolder holder = getSurfaceHolder();
            Canvas c = null;
            try {
                /* THE GPU WHERE THERE IS ONE. A software canvas re-composites a full-screen bitmap on the
                 * CPU thirty times a second; a hardware canvas uploads the two cached layers ONCE as
                 * textures and the frame is a few draw calls. 28+, not 26: before Pie a hardware canvas
                 * drops shadow layers on anything but text, and the sign's frame glows. */
                c = Build.VERSION.SDK_INT >= 28 ? holder.lockHardwareCanvas() : holder.lockCanvas();
                if (c != null && scene != null) {
                    if (pen == null) pen = new CanvasPen(c); else pen.setCanvas(c);
                    pen.frames = dance;
                    float shift = scene.parallaxPx(offset);
                    if (sky != null) c.drawBitmap(sky, 0, 0, null); else scene.drawSky(pen);
                    if (city != null) c.drawBitmap(city, -shift, cityTop, null); else scene.drawSkyline(pen, -shift, 0);
                    scene.drawFrame(pen, now(), offset, taps, 4);
                }
            } catch (Throwable t) {
                Log.w(TAG, "frame failed", t);
            } finally {
                if (c != null) { try { holder.unlockCanvasAndPost(c); } catch (Throwable ignored) { } }
            }
            handler.removeCallbacks(frame);
            long next = FramePolicy.nextDelayMs(visible, powerSave);
            if (next >= 0) handler.postDelayed(frame, next);
        }
    }

    // ------------------------------------------------------------------------------- the Pen

    /** {@link Pen} over an android.graphics.Canvas. Paints are reused: a frame allocates nothing. */
    static final class CanvasPen implements Pen {
        private Canvas c;
        private final Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint stroke = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final Paint text = new Paint(Paint.ANTI_ALIAS_FLAG);
        private final RectF r = new RectF();
        private final Paint[] scans = new Paint[3];
        private final float[] scanPitches = new float[3];
        private final int[] scanArgbs = new int[3];
        private final Paint spritePaint = new Paint(Paint.ANTI_ALIAS_FLAG | Paint.FILTER_BITMAP_FLAG);
        private final int[] tints = new int[3];
        private final ColorMatrixColorFilter[] tintFilters = new ColorMatrixColorFilter[3];
        private int tintNext = 0;
        /** PosterChan's dance frames (res/drawable-nodpi/pc_dance_N); null = draw without her. */
        Bitmap[] frames;

        CanvasPen(Canvas c) {
            this.c = c;
            fill.setStyle(Paint.Style.FILL);
            stroke.setStyle(Paint.Style.STROKE);
            text.setTypeface(Typeface.create(Typeface.MONOSPACE, Typeface.BOLD));
            text.setTextAlign(Paint.Align.CENTER);
        }

        void setCanvas(Canvas c) { this.c = c; }

        @Override public void fillRect(float x, float y, float w, float h, int argb) {
            fill.setShader(null); fill.setColor(argb); c.drawRect(x, y, x + w, y + h, fill);
        }

        @Override public void fillRectVGrad(float x, float y, float w, float h, int top, int bottom) {
            fill.setShader(new LinearGradient(0, y, 0, y + Math.max(1, h), top, bottom, Shader.TileMode.CLAMP));
            c.drawRect(x, y, x + w, y + h, fill);
            fill.setShader(null);
        }

        @Override public void fillCircleVGrad(float cx, float cy, float rad, int top, int bottom) {
            fill.setShader(new LinearGradient(0, cy - rad, 0, cy + rad, top, bottom, Shader.TileMode.CLAMP));
            c.drawCircle(cx, cy, rad, fill);
            fill.setShader(null);
        }

        @Override public void radialGlow(float cx, float cy, float rad, int inner) {
            fill.setShader(new RadialGradient(cx, cy, Math.max(1, rad), inner, inner & 0x00FFFFFF, Shader.TileMode.CLAMP));
            c.drawCircle(cx, cy, rad, fill);
            fill.setShader(null);
        }

        @Override public void line(float x1, float y1, float x2, float y2, float width, int argb) {
            stroke.setShader(null); stroke.setColor(argb); stroke.setStrokeWidth(width);
            stroke.clearShadowLayer();
            c.drawLine(x1, y1, x2, y2, stroke);
        }

        @Override public void strokeRect(float x, float y, float w, float h, float width, int argb, float glow) {
            stroke.setColor(argb); stroke.setStrokeWidth(width);
            if (glow > 0) stroke.setShadowLayer(glow, 0, 0, argb); else stroke.clearShadowLayer();
            c.drawRect(x, y, x + w, y + h, stroke);
            stroke.clearShadowLayer();
        }

        @Override public void text(String s, float cx, float baseline, float size, int argb, float glow) {
            text.setTextSize(size); text.setColor(argb);
            if (glow > 0) text.setShadowLayer(glow, 0, 0, argb); else text.clearShadowLayer();
            c.drawText(s, cx, baseline, text);
        }

        @Override public void ring(float cx, float cy, float rx, float ry, float width, int argb) {
            stroke.setColor(argb); stroke.setStrokeWidth(width); stroke.clearShadowLayer();
            r.set(cx - rx, cy - ry, cx + rx, cy + ry);
            c.drawOval(r, stroke);
        }

        /** One textured rect, not hundreds of lines: the pattern is a 1×pitch tile, built once. */
        @Override public void scanlines(float x, float y, float w, float h, int argb, float pitch) {
            // A frame uses TWO textures (the screen's and the hologram's): cache each, or switching
            // between them would build a bitmap every frame.
            int k = 0;
            while (k < scans.length && scans[k] != null && !(scanPitches[k] == pitch && scanArgbs[k] == argb)) k++;
            if (k == scans.length) k = 0;
            if (scans[k] == null || scanPitches[k] != pitch || scanArgbs[k] != argb) {
                int ph = Math.max(2, Math.round(pitch));
                Bitmap tile = Bitmap.createBitmap(1, ph, Bitmap.Config.ARGB_8888);
                tile.setPixel(0, 0, argb);
                Paint sp = new Paint();
                sp.setShader(new BitmapShader(tile, Shader.TileMode.REPEAT, Shader.TileMode.REPEAT));
                scans[k] = sp; scanPitches[k] = pitch; scanArgbs[k] = argb;
            }
            c.drawRect(x, y, x + w, y + h, scans[k]);
        }
        @Override public void sprite(int frame, float x, float y, float w, float h, float alpha, int tint, boolean flipV) {
            if (frames == null || frame < 0 || frame >= frames.length || frames[frame] == null) return;
            spritePaint.setAlpha(Math.max(0, Math.min(255, Math.round(255 * alpha))));
            spritePaint.setColorFilter(tint == 0 ? null : hologram(tint));
            r.set(x, y, x + w, y + h);
            if (flipV) { c.save(); c.scale(1f, -1f, 0f, y + h / 2f); }
            c.drawBitmap(frames[frame], null, r, spritePaint);
            if (flipV) c.restore();
        }
        /** Her light and shade in the tint's hue: out = tint * (0.25 + 1.2 * luminance). AwtPen.tint is the same. */
        private ColorMatrixColorFilter hologram(int tint) {
            for (int k = 0; k < tints.length; k++) if (tintFilters[k] != null && tints[k] == tint) return tintFilters[k];
            float tr = ((tint >> 16) & 255) / 255f, tg = ((tint >> 8) & 255) / 255f, tb = (tint & 255) / 255f;
            float[] m = new float[20];
            float[] t3 = {tr, tg, tb};
            for (int row = 0; row < 3; row++) {
                m[row * 5] = 0.3f * 1.2f * t3[row]; m[row * 5 + 1] = 0.59f * 1.2f * t3[row]; m[row * 5 + 2] = 0.11f * 1.2f * t3[row];
                m[row * 5 + 4] = 0.25f * 255f * t3[row];
            }
            m[18] = 1f;
            ColorMatrixColorFilter f = new ColorMatrixColorFilter(new ColorMatrix(m));
            int slot = tintNext++ % tints.length;
            tints[slot] = tint; tintFilters[slot] = f;
            return f;
        }
    }
}
