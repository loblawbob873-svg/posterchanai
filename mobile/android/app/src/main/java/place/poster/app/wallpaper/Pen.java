package place.poster.app.wallpaper;

/**
 * Everything the cyberpunk wallpaper draws with — and nothing else.
 *
 * The scene is written against this instead of android.graphics.Canvas so that the SAME drawing code
 * runs off-device: `tests/test_android_live_wallpaper.py` implements it over java.awt and renders the
 * real scene to PNGs, which is how a wallpaper gets looked at, measured and regression-tested on a
 * box with no emulator. The Android implementation is {@link CyberWallpaper.CanvasPen}.
 *
 * Colours are ARGB ints. Coordinates are pixels of whatever surface is being drawn.
 */
public interface Pen {
    void fillRect(float x, float y, float w, float h, int argb);

    /** A vertical gradient, `top` at y and `bottom` at y+h. */
    void fillRectVGrad(float x, float y, float w, float h, int top, int bottom);

    /** A disc filled with a vertical gradient — the sun. */
    void fillCircleVGrad(float cx, float cy, float r, int top, int bottom);

    /** A soft radial glow: `inner` at the centre, fully transparent at radius r. */
    void radialGlow(float cx, float cy, float r, int inner);

    void line(float x1, float y1, float x2, float y2, float width, int argb);

    /** An outlined rectangle; `glow` (px) > 0 adds a neon halo in the same colour. */
    void strokeRect(float x, float y, float w, float h, float width, int argb, float glow);

    /** Bold monospace text centred on cx; `glow` (px) > 0 adds a neon halo in the same colour. */
    void text(String s, float cx, float baseline, float size, int argb, float glow);

    /** An outlined ellipse. */
    void ring(float cx, float cy, float rx, float ry, float width, int argb);

    /** Horizontal 1px lines every `pitch` px over the area — the CRT texture. */
    void scanlines(float x, float y, float w, float h, int argb, float pitch);

    /**
     * Dance frame `frame` (0..{@link CyberScene#FRAMES}-1) of PosterChan, scaled into the box at `alpha`.
     * `tint` 0 draws her as she is; any other colour draws her as a HOLOGRAM in that colour (her light
     * and shade, in that hue). `flipV` mirrors her top to bottom about the box — a reflection.
     * Must do nothing (not throw) when the frames are not available.
     */
    void sprite(int frame, float x, float y, float w, float h, float alpha, int tint, boolean flipV);
}
