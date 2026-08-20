package place.poster.app.phone;

import android.content.Context;
import android.graphics.Canvas;
import android.graphics.ColorFilter;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.RadialGradient;
import android.graphics.Rect;
import android.graphics.Shader;
import android.graphics.drawable.Drawable;

import place.poster.app.ui.PcTheme;
import place.poster.app.ui.Skin;

/**
 * A DIALPAD KEY THAT LIGHTS UP WHEN YOU PRESS IT.
 *
 * The dialpad is the surface people judge a phone by, and a press that produces nothing but a grey
 * ripple is what makes a hand-rolled dialer feel cheap. A ripple is also the wrong instrument here:
 * it is a spreading grey circle designed to be neutral, and this app's flagship theme is called
 * Cyberpunk.
 *
 * So the key is drawn rather than styled. Pressed, it gains a bloom outside the rim, a bright ring,
 * and a lit interior — all in the palette's accent, all drawn with concentric strokes of falling
 * alpha rather than a BlurMaskFilter, which needs a software layer under hardware acceleration and
 * silently draws nothing without one.
 *
 * IT DEGRADES, and that is not a detail. On the light palettes (`p.neon` false — Professional,
 * Windows 98, Cherry Blossom) a bloom behind dark text destroys it, which is the same readability
 * rule client.css enforces by turning every text-shadow off outside the flagship. There the pressed
 * state is a firm, flat colour change instead: unmistakable, and still legible.
 *
 * STATEFUL, which is the part that is easy to get wrong: a Drawable only ever hears about a press if
 * it says it is stateful AND returns true from onStateChange to ask for a redraw. Return false and
 * the state is recorded, nothing repaints, and the key never lights — with nothing in any log.
 */
public class KeyGlow extends Drawable {

    private final Context ctx;
    private final PcTheme.Palette pal;
    private final Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint ring = new Paint(Paint.ANTI_ALIAS_FLAG);
    private final Paint bloom = new Paint(Paint.ANTI_ALIAS_FLAG);
    private boolean pressed;

    public KeyGlow(Context ctx, PcTheme.Palette pal) {
        this.ctx = ctx;
        this.pal = pal;
        ring.setStyle(Paint.Style.STROKE);
        bloom.setStyle(Paint.Style.STROKE);
    }

    @Override public boolean isStateful() { return true; }

    @Override
    protected boolean onStateChange(int[] state) {
        boolean down = false;
        if (state != null) {
            for (int s : state) if (s == android.R.attr.state_pressed) down = true;
        }
        if (down == pressed) return false;
        pressed = down;
        invalidateSelf();
        return true;                 // "I repainted" — return false and the key never lights.
    }

    @Override
    public void draw(Canvas canvas) {
        Rect b = getBounds();
        if (b.width() <= 0 || b.height() <= 0) return;
        float cx = b.exactCenterX(), cy = b.exactCenterY();
        // Inset so the bloom has somewhere to go without being clipped by the cell.
        float r = Math.min(b.width(), b.height()) / 2f - Skin.dp(ctx, 4);
        if (r <= 0) return;

        if (pressed && pal.neon) {
            // THE BLOOM. Concentric strokes of falling alpha, outward from the rim — a blur without
            // a BlurMaskFilter, which under hardware acceleration needs a software layer and draws
            // nothing without one.
            int steps = 7;
            for (int i = steps; i >= 1; i--) {
                float t = i / (float) steps;
                bloom.setStrokeWidth(Skin.dp(ctx, 2));
                bloom.setColor(Skin.alpha(pal.accent, 0.16 * (1 - t) + 0.02));
                canvas.drawCircle(cx, cy, r + Skin.dp(ctx, 1.6f) * i, bloom);
            }
            // A lit interior, brightest at the middle, so the key reads as a source rather than an
            // outline.
            fill.setShader(new RadialGradient(cx, cy, r,
                    Skin.alpha(pal.accent, 0.55), Skin.alpha(pal.accent, 0.18),
                    Shader.TileMode.CLAMP));
            canvas.drawCircle(cx, cy, r, fill);
            fill.setShader(null);
            ring.setStrokeWidth(Skin.dp(ctx, 2));
            ring.setColor(pal.accent);
            canvas.drawCircle(cx, cy, r, ring);
            return;
        }

        if (pressed) {
            // The light-theme press: flat, firm, and legible. No halo behind dark text.
            fill.setColor(Skin.alpha(pal.accent, 0.34));
            canvas.drawCircle(cx, cy, r, fill);
            ring.setStrokeWidth(Skin.dp(ctx, 2));
            ring.setColor(pal.accent);
            canvas.drawCircle(cx, cy, r, ring);
            return;
        }

        fill.setColor(Skin.alpha(pal.accent, pal.isDark() ? 0.10 : 0.08));
        canvas.drawCircle(cx, cy, r, fill);
        ring.setStrokeWidth(Skin.dp(ctx, 1));
        ring.setColor(Skin.alpha(pal.accent, 0.28));
        canvas.drawCircle(cx, cy, r, ring);
    }

    /** For the digit itself: lit while the key is down, on a theme that glows. */
    public boolean lit() { return pressed; }

    @Override public void setAlpha(int a) { }
    @Override public void setColorFilter(ColorFilter cf) { }
    @Override public int getOpacity() { return PixelFormat.TRANSLUCENT; }
}
