package place.poster.app.ui;

import android.content.Context;
import android.content.res.ColorStateList;
import android.graphics.Canvas;
import android.graphics.Color;
import android.graphics.ColorFilter;
import android.graphics.LinearGradient;
import android.graphics.Paint;
import android.graphics.PixelFormat;
import android.graphics.RadialGradient;
import android.graphics.Rect;
import android.graphics.Shader;
import android.graphics.drawable.Drawable;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.graphics.drawable.StateListDrawable;
import android.os.Build;
import android.util.TypedValue;
import android.widget.TextView;

/**
 * A PALETTE, TURNED INTO PIXELS. Everything the native screens draw comes from here, so that adding
 * a tenth theme to client.css means adding a tenth row to PcTheme and nothing else.
 *
 * Programmatic drawables rather than XML for exactly that reason: nine themes x every surface is
 * ~200 drawable files that would have to be kept in step by hand, and the one thing this codebase
 * has learned about two copies of a value is that they drift silently. A GradientDrawable built from
 * a Palette cannot.
 */
public final class Skin {

    private Skin() { }

    public static int dp(Context c, float v) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v,
                c.getResources().getDisplayMetrics()));
    }

    /** The colour with a different alpha, 0..1. Used everywhere a CSS rule would say rgba(var(--x), .3). */
    public static int alpha(int argb, double a) {
        int al = (int) Math.round(Math.max(0, Math.min(1, a)) * 255.0);
        return (al << 24) | (argb & 0x00FFFFFF);
    }

    /** The colour with its alpha multiplied — used to turn an opaque page into a scrim. */
    public static int scale(int argb, double factor) {
        int al = (int) Math.round(Color.alpha(argb) * Math.max(0, Math.min(1, factor)));
        return (al << 24) | (argb & 0x00FFFFFF);
    }

    /** Two colours mixed, t=0 gives a, t=1 gives b. For a pressed state that suits any palette. */
    public static int mix(int a, int b, double t) {
        double u = 1 - t;
        return Color.argb(
            (int) (Color.alpha(a) * u + Color.alpha(b) * t),
            (int) (Color.red(a) * u + Color.red(b) * t),
            (int) (Color.green(a) * u + Color.green(b) * t),
            (int) (Color.blue(a) * u + Color.blue(b) * t));
    }

    /**
     * THE PAGE. A vertical bg→bg2 wash, the two ambient corner glows the stylesheet paints with
     * radial-gradient, and — on the flagship theme only — the grid and the scanlines.
     *
     * The decor is gated on the palette, not on a setting, for the reason client.css gates it: a
     * neon grid over Cherry Blossom or Windows 98 does not read as a theme, it reads as a bug.
     */
    public static Drawable page(final PcTheme.Palette p) { return page(p, 1.0); }

    /**
     * The same page at reduced opacity — a SCRIM. The launcher needs this and nothing else does: a
     * home screen must let the person's wallpaper through (that is half of what a home screen IS),
     * but white-on-anything is unreadable over an arbitrary photograph. A themed wash at ~half alpha
     * is what makes both true at once, and it is why the launcher's window really does declare
     * windowShowWallpaper rather than painting over it.
     */
    public static Drawable page(final PcTheme.Palette p, final double opacity) {
        return new Drawable() {
            private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
            private final Paint line = new Paint(Paint.ANTI_ALIAS_FLAG);

            @Override public void draw(Canvas canvas) {
                Rect b = getBounds();
                if (b.width() <= 0 || b.height() <= 0) return;

                paint.setShader(new LinearGradient(0, 0, 0, b.height(),
                        scale(p.bg, opacity), scale(p.bg2, opacity), Shader.TileMode.CLAMP));
                canvas.drawRect(b, paint);

                // --amb1: an ellipse at 50% -8%, i.e. mostly off the top of the page.
                if (Color.alpha(p.amb1) > 0) {
                    paint.setShader(new RadialGradient(b.width() * 0.5f, b.height() * -0.08f,
                            Math.max(1, b.width() * 0.95f), p.amb1, 0x00000000, Shader.TileMode.CLAMP));
                    canvas.drawRect(b, paint);
                }
                // --amb2: bottom right.
                if (Color.alpha(p.amb2) > 0) {
                    paint.setShader(new RadialGradient(b.width(), b.height(),
                            Math.max(1, b.width() * 0.9f), p.amb2, 0x00000000, Shader.TileMode.CLAMP));
                    canvas.drawRect(b, paint);
                }
                paint.setShader(null);

                if (!p.decor) return;
                // The neon grid, faint enough to be texture rather than pattern.
                line.setStyle(Paint.Style.STROKE);
                line.setStrokeWidth(1f);
                line.setColor(alpha(p.accent, 0.055));
                float step = b.height() / 26f;
                if (step >= 8f) {
                    for (float y = b.top; y < b.bottom; y += step) canvas.drawLine(b.left, y, b.right, y, line);
                    for (float x = b.left; x < b.right; x += step) canvas.drawLine(x, b.top, x, b.bottom, line);
                }
                // CRT scanlines: 1px every 3px, dark, very low alpha.
                line.setColor(0x14000000);
                for (float y = b.top; y < b.bottom; y += 3f) canvas.drawLine(b.left, y, b.right, y, line);
            }

            @Override public void setAlpha(int a) { }
            @Override public void setColorFilter(ColorFilter cf) { }
            @Override public int getOpacity() {
                return opacity >= 0.999 ? PixelFormat.OPAQUE : PixelFormat.TRANSLUCENT;
            }
        };
    }

    /** A card: the panel surface, the theme's corner radius, and the hairline the stylesheet calls --line. */
    public static GradientDrawable panel(Context c, PcTheme.Palette p) {
        GradientDrawable g = new GradientDrawable();
        g.setShape(GradientDrawable.RECTANGLE);
        g.setColor(opaque(p.panel, p.bg));
        g.setCornerRadius(dp(c, p.radiusDp));
        g.setStroke(Math.max(1, dp(c, 1)), p.line);
        return g;
    }

    /**
     * A translucent CSS surface composited over the page colour.
     *
     * A GradientDrawable with an alpha fill really is see-through, and what shows through in a native
     * view is whatever the parent painted — which for a list row scrolling over the page background
     * means the row changes colour as it moves. Flattening against --bg is what makes a panel look
     * like the stylesheet's panel instead of like a bug.
     */
    public static int opaque(int argb, int over) {
        double a = Color.alpha(argb) / 255.0;
        if (a >= 0.999) return argb;
        return 0xFF000000 | (mix(over, argb | 0xFF000000, a) & 0x00FFFFFF);
    }

    /** A filled pill — the send button, the answer button, a dial key. */
    public static Drawable pill(Context c, PcTheme.Palette p, int fill, boolean round) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(fill);
        g.setCornerRadius(round ? dp(c, 999) : dp(c, Math.max(6, p.radiusDp)));
        return press(c, g, fill);
    }

    /** An outlined pill — a secondary action that must not shout. */
    public static Drawable ghost(Context c, PcTheme.Palette p, int stroke, boolean round) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(alpha(stroke, 0.10));
        g.setStroke(Math.max(1, dp(c, 1.5f)), alpha(stroke, 0.55));
        g.setCornerRadius(round ? dp(c, 999) : dp(c, Math.max(6, p.radiusDp)));
        return press(c, g, stroke);
    }

    /**
     * A pressed state that works on every palette. A ripple where the platform has one (API 21+),
     * and a darkened/lightened copy where it does not — never nothing, because a key with no press
     * feedback feels broken in a way people describe as "it didn't register".
     */
    private static Drawable press(Context c, GradientDrawable base, int tint) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            return new RippleDrawable(ColorStateList.valueOf(alpha(tint, 0.35)), base, null);
        }
        GradientDrawable down = (GradientDrawable) base.getConstantState().newDrawable().mutate();
        down.setColor(mix(tint, 0xFF000000, 0.25));
        StateListDrawable s = new StateListDrawable();
        s.addState(new int[]{ android.R.attr.state_pressed }, down);
        s.addState(new int[0], base);
        return s;
    }

    /**
     * A message bubble. Ours takes the accent, theirs takes the panel — and both get the theme's
     * radius with the corner nearest the speaker squared off, which is what makes a thread read as a
     * conversation rather than as a list of boxes.
     */
    public static Drawable bubble(Context c, PcTheme.Palette p, boolean mine) {
        GradientDrawable g = new GradientDrawable();
        float r = dp(c, Math.max(8, p.radiusDp + 4));
        float tip = dp(c, Math.max(2, p.radiusDp / 4f));
        g.setCornerRadii(mine
                ? new float[]{ r, r, r, r, tip, tip, r, r }
                : new float[]{ r, r, r, r, r, r, tip, tip });
        if (mine) {
            g.setColor(alpha(p.accent, p.isDark() ? 0.22 : 0.16));
            g.setStroke(Math.max(1, dp(c, 1)), alpha(p.accent, 0.55));
        } else {
            g.setColor(opaque(p.panel2, p.bg));
            g.setStroke(Math.max(1, dp(c, 1)), p.line);
        }
        return g;
    }

    /**
     * The neon halo on a heading. Only where the theme actually glows — client.css switches every
     * text-shadow off for the non-flagship themes, and it is not a stylistic choice: a halo behind
     * dark text on a light background destroys it.
     */
    public static void glow(TextView t, PcTheme.Palette p) {
        if (t == null) return;
        if (p.neon) t.setShadowLayer(dp(t.getContext(), 8), 0, 0, alpha(p.accent, 0.55));
        else t.setShadowLayer(0, 0, 0, 0);
    }

    /**
     * A LEGIBILITY SHADOW, for text drawn over the person's wallpaper. Not the theme's neon glow —
     * that halo is an accent colour and disappears against half the photographs anyone would pick.
     * This one is simply the opposite of the text: dark text gets a white halo, light text a black
     * one, so an icon label survives being dropped on a white sky or a black night shot.
     */
    public static void legible(TextView t, PcTheme.Palette p) {
        if (t == null) return;
        boolean lightText = PcTheme.luminance(p.text) > 0.5;
        t.setShadowLayer(dp(t.getContext(), 3), 0, 1, lightText ? 0xE6000000 : 0xE6FFFFFF);
    }

    /**
     * A SPRITE ICON IN A PALETTE COLOUR — and the one place that tinting is done.
     *
     * `ContextCompat.getDrawable` rather than `Resources.getDrawable`, and `DrawableCompat.setTint`
     * rather than `setColorFilter`, because both are the documented route for a VectorDrawable and
     * the plain calls are the ones that quietly return or draw nothing on some platform versions.
     * That is what "a lot of the PosterChan apps in the launcher are empty circles" was: the pill
     * background drawn and no glyph inside it.
     *
     * Returns null only when the resource genuinely is not there — and every caller has a visible
     * fallback for that (see `letter`), because a blank icon is the one outcome that tells the
     * person nothing at all.
     */
    public static Drawable icon(Context c, int res, int color) {
        if (res == 0) return null;
        try {
            Drawable d = androidx.core.content.ContextCompat.getDrawable(c, res);
            if (d == null) return null;
            // colour 0 means LEAVE IT ALONE — the app's own launcher icon is already coloured, and
            // tinting it would produce a solid silhouette.
            if (color == 0) return d;
            d = androidx.core.graphics.drawable.DrawableCompat.wrap(d.mutate());
            androidx.core.graphics.drawable.DrawableCompat.setTint(d, color);
            return d;
        } catch (Throwable t) {
            return null;
        }
    }

    /**
     * WHAT TO DRAW WHEN THERE IS NO ICON: the first letter, in the theme's colours.
     *
     * Never nothing. A missing glyph inside a coloured circle is indistinguishable from a broken
     * launcher, and it is the shape this was reported in — so the fallback is something that
     * identifies the app rather than an absence that identifies a bug.
     */
    public static Drawable letter(final Context c, final PcTheme.Palette p, String label) {
        final String ch = (label == null || label.trim().isEmpty())
                ? "?" : label.trim().substring(0, 1).toUpperCase(java.util.Locale.ROOT);
        final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
        paint.setColor(p.accent);
        paint.setTextAlign(Paint.Align.CENTER);
        paint.setFakeBoldText(true);
        return new Drawable() {
            @Override public void draw(Canvas canvas) {
                Rect b = getBounds();
                if (b.width() <= 0) return;
                paint.setTextSize(b.height() * 0.62f);
                Paint.FontMetrics fm = paint.getFontMetrics();
                float y = b.centerY() - (fm.ascent + fm.descent) / 2f;
                canvas.drawText(ch, b.centerX(), y, paint);
            }
            @Override public void setAlpha(int a) { paint.setAlpha(a); }
            @Override public void setColorFilter(ColorFilter cf) { paint.setColorFilter(cf); }
            @Override public int getOpacity() { return PixelFormat.TRANSLUCENT; }
        };
    }

    /**
     * A NEON EDGE — a hairline in the accent with a bloom under it, for the bottom of a header bar
     * or the top of a compose bar.
     *
     * The flagship theme is called Cyberpunk and a flat panel with a 1px grey line is not it. On a
     * theme that does not glow (`p.neon` false — every light palette) this degrades to exactly that
     * hairline, because a bloom behind dark text on a light background is the readability bug
     * client.css turns every text-shadow off to avoid.
     */
    public static Drawable edge(final Context c, final PcTheme.Palette p, final boolean top) {
        final int h = dp(c, p.neon ? 10 : 1);
        return new Drawable() {
            private final Paint paint = new Paint(Paint.ANTI_ALIAS_FLAG);
            @Override public void draw(Canvas canvas) {
                Rect b = getBounds();
                if (b.width() <= 0) return;
                float y = top ? b.top : b.bottom - dp(c, 1);
                if (p.neon) {
                    paint.setShader(new LinearGradient(0, top ? b.top : b.bottom - h,
                            0, top ? b.top + h : b.bottom,
                            top ? new int[]{ alpha(p.accent, 0.30), 0 }
                                : new int[]{ 0, alpha(p.accent, 0.30) },
                            null, Shader.TileMode.CLAMP));
                    canvas.drawRect(b.left, top ? b.top : b.bottom - h,
                                    b.right, top ? b.top + h : b.bottom, paint);
                    paint.setShader(null);
                }
                paint.setColor(p.neon ? alpha(p.accent, 0.75) : p.line);
                canvas.drawRect(b.left, y, b.right, y + dp(c, 1), paint);
            }
            @Override public void setAlpha(int a) { }
            @Override public void setColorFilter(ColorFilter cf) { }
            @Override public int getOpacity() { return PixelFormat.TRANSLUCENT; }
        };
    }

    /**
     * A HEADER SURFACE: the panel, with the neon edge under it. One call, so every bar in the native
     * screens is the same object rather than four places that drift.
     */
    public static Drawable bar(Context c, PcTheme.Palette p, boolean edgeOnTop) {
        GradientDrawable fill = new GradientDrawable();
        fill.setColor(opaque(p.panel2, p.bg));
        return new android.graphics.drawable.LayerDrawable(
                new Drawable[]{ fill, edge(c, p, edgeOnTop) });
    }

    /**
     * A SECTION HEADING in the flagship's voice: small, letter-spaced, muted, and glowing where the
     * theme glows. Applied rather than styled per screen so "RECENTS" looks the same everywhere.
     */
    public static void heading(TextView t, PcTheme.Palette p) {
        if (t == null) return;
        t.setTextColor(p.muted);
        t.setAllCaps(true);
        t.setTextSize(11);
        try { t.setLetterSpacing(0.14f); } catch (Throwable ignored) { }
        if (p.neon) t.setShadowLayer(dp(t.getContext(), 6), 0, 0, alpha(p.accent, 0.45));
    }

    /**
     * A GLASS SURFACE over the wallpaper — the dock, and the drawer behind it.
     *
     * Both were a flat black rectangle, which is what `--bg` is on the flagship theme and reads as
     * "unstyled" rather than as dark: "the black dock looks too plain" and "the app drawer is also
     * black and unstylish". The client has a real identity — translucency, a hairline, a bloom — and
     * the phone shell was inheriting none of it.
     *
     * So: the panel tint at partial alpha so the wallpaper shows through, a hairline in the accent,
     * and on a theme that glows a bloom along the lit edge. Rounded to the theme's own radius, which
     * keeps Windows 98 square because square is what that theme is.
     */
    public static Drawable glass(final Context c, final PcTheme.Palette p,
                                 final double opacity, final boolean edgeTop) {
        final float r = dp(c, Math.max(0, p.radiusDp + 6));
        return new Drawable() {
            private final Paint fill = new Paint(Paint.ANTI_ALIAS_FLAG);
            private final Paint line = new Paint(Paint.ANTI_ALIAS_FLAG);
            @Override public void draw(Canvas canvas) {
                Rect b = getBounds();
                if (b.width() <= 0 || b.height() <= 0) return;
                // A vertical lift, so the surface reads as glass rather than as a painted slab.
                fill.setShader(new LinearGradient(0, b.top, 0, b.bottom,
                        scale(opaque(p.panel2, p.bg), opacity + 0.06),
                        scale(opaque(p.panel, p.bg), opacity), Shader.TileMode.CLAMP));
                canvas.drawRoundRect(b.left, b.top, b.right, b.bottom, r, r, fill);
                fill.setShader(null);
                if (p.neon) {
                    // The bloom along the lit edge. Concentric strokes rather than a BlurMaskFilter,
                    // which needs a software layer under hardware acceleration and draws nothing
                    // without one.
                    line.setStyle(Paint.Style.STROKE);
                    for (int i = 5; i >= 1; i--) {
                        line.setStrokeWidth(dp(c, 1.5f) * i);
                        line.setColor(alpha(p.accent, 0.05 * (1.0 / i)));
                        canvas.drawRoundRect(b.left, b.top, b.right, b.bottom, r, r, line);
                    }
                }
                line.setStyle(Paint.Style.STROKE);
                line.setStrokeWidth(Math.max(1, dp(c, 1)));
                line.setColor(p.neon ? alpha(p.accent, 0.42) : p.line);
                canvas.drawRoundRect(b.left, b.top, b.right, b.bottom, r, r, line);
                // The lit hairline along one edge — top for a drawer, top for a dock, so the eye
                // reads it as a surface that has come up from below.
                if (p.neon) {
                    float y = edgeTop ? b.top + dp(c, 1) : b.bottom - dp(c, 1);
                    line.setStrokeWidth(dp(c, 1.5f));
                    line.setColor(alpha(p.accent, 0.8));
                    canvas.drawLine(b.left + r, y, b.right - r, y, line);
                }
            }
            @Override public void setAlpha(int a) { }
            @Override public void setColorFilter(ColorFilter cf) { }
            @Override public int getOpacity() { return PixelFormat.TRANSLUCENT; }
        };
    }

    /** A 1px divider in the theme's line colour, for a list. */
    public static Drawable divider(Context c, PcTheme.Palette p) {
        GradientDrawable g = new GradientDrawable();
        g.setColor(p.line);
        g.setSize(0, Math.max(1, dp(c, 1)));
        return g;
    }

    /**
     * The circle behind a contact's initials, coloured from the name so the same person is the same
     * colour every time. Hue only — saturation and lightness come from the palette, so an avatar can
     * never come out unreadable on a light theme or invisible on a dark one.
     */
    public static Drawable avatar(Context c, PcTheme.Palette p, String name) {
        GradientDrawable g = new GradientDrawable();
        g.setShape(GradientDrawable.OVAL);
        g.setColor(avatarColor(p, name));
        return g;
    }

    /** Split out and free of Context so tests can check the "same name, same colour" rule. */
    public static int avatarColor(PcTheme.Palette p, String name) {
        int h = 0;
        String s = name == null ? "" : name;
        for (int i = 0; i < s.length(); i++) h = h * 31 + s.charAt(i);
        float hue = ((h % 360) + 360) % 360;
        float[] hsv = new float[]{ hue, p.isDark() ? 0.55f : 0.45f, p.isDark() ? 0.55f : 0.85f };
        return Color.HSVToColor(p.isDark() ? 0xFF : 0xFF, hsv);
    }
}
