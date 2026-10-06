package place.poster.app.wallpaper;

/**
 * The PosterChan cyberpunk city: a neon sun sliced by the horizon, a skyline with lit windows and a
 * rooftop "POSTERCHAN" sign, a perspective grid floor running toward you, a thin digital rain, and CRT
 * scanlines over all of it. The palette is the app's own cyberpunk theme ({@code PcTheme} / client.css
 * `--neon` / `--neon2`), so the home screen and the app read as one thing.
 *
 * PURE: no Android, no allocation per frame, no randomness that is not derived from a seed — the same
 * seed and the same `t` draw the same frame, which is what lets a test render it and assert on pixels.
 *
 * DRAWN IN TWO HALVES, because a wallpaper is drawn thirty times a second for hours:
 *   * STATIC — {@link #drawSky} (sky, stars, sun, the floor's base) and {@link #drawSkyline} (buildings,
 *     every window, the sign's frame). The Android engine renders these ONCE per surface size into
 *     bitmaps and blits them.
 *   * DYNAMIC — {@link #drawFrame}: the grid, the rain, a few windows flickering, the sign's lit text, tap
 *     pulses and the scanlines. A few dozen primitives per frame.
 *
 * The skyline is {@link #SPAN} screens wide and slides with the home screen's page (parallax); the sky
 * does not move, which is what makes it read as distance.
 */
public final class CyberScene {

    // The cyberpunk theme, verbatim from PcTheme's default palette.
    public static final int BG = 0xFF0A0A0F;
    public static final int CYAN = 0xFF3CE8FF;      // --neon
    public static final int MAGENTA = 0xFFFF5CF0;   // --neon2
    public static final int GREEN = 0xFF00FF88;
    public static final int GOLD = 0xFFFFCF2B;
    static final int SKY_TOP = 0xFF06050C;
    static final int SKY_HORIZON = 0xFF2B0C40;
    static final int FLOOR_NEAR = 0xFF050309;
    static final int FLOOR_FAR = 0xFF1A0629;
    static final int BUILDING = 0xFF0C0B16;
    static final int BUILDING_EDGE = 0xFF16142A;
    static final int WINDOW_WARM = 0xFFFFCF6B;
    static final int WINDOW_COLD = 0xFF7FEFFF;

    /** How many screens wide the skyline is: the room parallax has to move in. */
    public static final float SPAN = 1.35f;
    public static final String SIGN = "POSTERCHAN";

    public final float w, h, horizon;
    final long seed;

    // Buildings, in SKYLINE coordinates (x from 0 to SPAN*w; heights above the horizon).
    final int count;
    final float[] bx, bw, bh;
    final int[] bAccent;
    final int signIdx;
    final float signX, signY, signW, signH;   // in skyline coordinates
    final float letter;                       // the sign's glyph size
    final float winW, winH, winGapX, winGapY;

    public CyberScene(float w, float h, long seed) {
        this.w = Math.max(1f, w);
        this.h = Math.max(1f, h);
        this.seed = seed;
        this.horizon = this.h * 0.62f;
        float unit = Math.min(this.w, this.h);
        winW = Math.max(2f, unit * 0.010f);
        winH = Math.max(2f, unit * 0.014f);
        winGapX = winW * 0.9f;
        winGapY = winH * 0.8f;

        // Lay the buildings out left to right until the span is full. A hard upper bound keeps a
        // pathological size from allocating without limit.
        float span = SPAN * this.w;
        float[] xs = new float[256], ws = new float[256], hs = new float[256];
        int[] acc = new int[256];
        int n = 0;
        float x = -unit * 0.02f;
        while (x < span && n < 256) {
            float bwid = this.w * (0.045f + 0.075f * rnd(n, 1));
            float bht = this.h * (0.07f + 0.23f * (float) Math.pow(rnd(n, 2), 1.4));
            // A VALLEY where the sun is: buildings near the middle of the span are cut down, so the sun
            // rises through a gap instead of being walled off — the composition every synthwave city uses.
            float fromMid = Math.abs(x + bwid / 2f - span / 2f) / (this.w * 0.42f);
            bht *= 0.30f + 0.70f * Math.min(1f, fromMid * fromMid);
            xs[n] = x; ws[n] = bwid; hs[n] = bht;
            acc[n] = rnd(n, 3) < 0.5f ? CYAN : MAGENTA;
            x += bwid + this.w * (0.002f + 0.012f * rnd(n, 4));
            n++;
        }
        count = n;
        bx = copy(xs, n); bw = copy(ws, n); bh = copy(hs, n); bAccent = copy(acc, n);

        // THE SIGN HANGS VERTICALLY off a tall tower left of centre — a stacked Blade Runner sign, so it
        // is on the middle home page without walling off the sun. The tower is made tall enough to carry it.
        float want = span / 2f - this.w * 0.26f;
        int best = 0; float bestD = Float.MAX_VALUE;
        for (int i = 0; i < count; i++) {
            float d = Math.abs(bx[i] + bw[i] / 2f - want);
            if (d < bestD) { bestD = d; best = i; }
        }
        signIdx = best;
        bw[best] = Math.max(bw[best], this.w * 0.12f);
        bh[best] = Math.max(bh[best], this.h * 0.43f);
        letter = this.h * 0.027f;
        signW = letter * 1.55f;
        signH = SIGN.length() * letter * 1.12f + letter * 0.7f;
        signX = bx[best] + bw[best] - signW * 0.35f;
        signY = horizon - bh[best] + this.h * 0.025f;
    }

    // ---------------------------------------------------------------------------- static layers

    /** Sky, stars, sun and the floor's base gradient — everything that never moves. Screen coordinates. */
    public void drawSky(Pen p) {
        p.fillRectVGrad(0, 0, w, horizon, SKY_TOP, SKY_HORIZON);

        // Stars: sparse, mostly high, a few brighter.
        for (int i = 0; i < 90; i++) {
            float sx = rnd(i, 11) * w;
            float sy = (float) Math.pow(rnd(i, 12), 1.7) * horizon * 0.85f;
            float s = Math.max(1f, Math.min(w, h) * (rnd(i, 13) < 0.1f ? 0.004f : 0.0022f));
            int a = (int) (90 + 140 * rnd(i, 14));
            p.fillRect(sx, sy, s, s, (a << 24) | 0xE8EAFF);
        }

        // The sun, sitting on the horizon behind the city, with its glow.
        float r = Math.min(w, h) * 0.36f;
        float cx = w / 2f, cy = horizon - r * 0.62f;
        p.radialGlow(cx, cy, r * 2.1f, 0x55FF2FD8);
        p.fillCircleVGrad(cx, cy, r, GOLD, MAGENTA);
        // The slices: bands of sky across the sun, starting just above its middle and thickening toward
        // the horizon — placed where the skyline's central valley leaves the sun VISIBLE, or they are
        // drawn and never seen.
        for (int k = 0; k < 8; k++) {
            float f = k / 8f;
            float by = cy - r * 0.18f + r * 1.05f * (float) Math.pow(f, 1.25);
            float bhgt = r * (0.014f + 0.055f * f);
            if (by > horizon) break;
            p.fillRect(cx - r - 2, by, 2 * r + 4, bhgt, skyAt(by + bhgt / 2f));
        }

        // The floor: deep violet at the horizon, almost black at your feet.
        p.fillRectVGrad(0, horizon, w, h - horizon, FLOOR_FAR, FLOOR_NEAR);
        // The horizon line itself, lit.
        p.line(0, horizon, w, horizon, Math.max(1.5f, h * 0.0015f), 0xCCFF5CF0);
    }

    /**
     * Buildings, every window, the sign's box and unlit letters — offset by (dx, dy). The engine renders
     * this ONCE into a bitmap that starts at {@link #skylineTop()}, so it passes dy = -skylineTop() and
     * blits the bitmap back at that height, shifted by the parallax.
     */
    public void drawSkyline(Pen p, float dx, float dy) {
        for (int i = 0; i < count; i++) {
            float x = bx[i] + dx, top = horizon - bh[i] + dy, bot = horizon + dy;
            p.fillRect(x, top, bw[i], bot - top, BUILDING);
            // A lit edge on the roofline: the neon that makes it a cyberpunk skyline and not a bar chart.
            p.line(x, top, x + bw[i], top, Math.max(1.5f, h * 0.0016f), alpha(bAccent[i], 0.85f));
            p.line(x, top, x, bot, Math.max(1f, h * 0.0008f), BUILDING_EDGE);
            // Windows: a grid, a seeded quarter of them lit, mostly warm, some cold.
            int cols = cols(i), rows = rows(i);
            for (int c = 0; c < cols; c++) {
                for (int rIdx = 0; rIdx < rows; rIdx++) {
                    float lit = rnd3(i, c, rIdx);
                    if (lit > 0.26f) continue;
                    int col = rnd3(i, rIdx + 97, c) < 0.75f ? WINDOW_WARM : WINDOW_COLD;
                    float a = 0.35f + 0.55f * rnd3(i, c + 31, rIdx + 7);
                    p.fillRect(winX(i, c) + dx, winY(i, rIdx) + dy, winW, winH, alpha(col, a));
                }
            }
        }
        // The sign's box, frame and UNLIT letters. The lit letters are dynamic (they flicker).
        p.fillRect(signX + dx, signY + dy, signW, signH, 0xF0090812);
        p.strokeRect(signX + dx, signY + dy, signW, signH, Math.max(2f, h * 0.0022f), alpha(MAGENTA, 0.9f),
                h * 0.006f);
        letters(p, signX + dx, dy, alpha(CYAN, 0.16f), 0);
    }

    // ------------------------------------------------------------------------------- the frame

    /** How far the skyline slides for a home-screen page offset in [0, 1]. */
    public float parallaxPx(float offset) {
        float o = Math.max(0f, Math.min(1f, offset));
        return o * (SPAN - 1f) * w;
    }

    /**
     * Everything that moves. `t` seconds since the wallpaper started, `offset` the home screen's page
     * offset in [0,1], `taps` as {x, y, tStart} triples (screen coordinates), `nTaps` of them in use.
     */
    public void drawFrame(Pen p, float t, float offset, float[] taps, int nTaps) {
        float shift = parallaxPx(offset);
        drawGrid(p, t, shift);
        drawRain(p, t);
        drawFlicker(p, t, shift);
        drawSign(p, t, shift);
        for (int i = 0; i + 2 < (taps == null ? 0 : nTaps * 3); i += 3) drawPulse(p, taps[i], taps[i + 1], t - taps[i + 2]);
        p.scanlines(0, 0, w, h, 0x26000000, Math.max(2f, h / 640f));
    }

    void drawGrid(Pen p, float t, float shift) {
        float vpX = w / 2f, depth = h - horizon;
        // Horizontal lines rushing toward you: a line's distance runs 0 → 1 and wraps, and its screen y
        // is perspective — bunched at the horizon, spreading at the bottom.
        float phase = (t * 0.32f) % 1f;
        int rowsN = 13;
        for (int i = 0; i < rowsN; i++) {
            float f = (i + phase) / rowsN;
            float y = horizon + depth * f * f;
            float a = 0.15f + 0.75f * f;
            p.line(0, y, w, y, Math.max(3f, h * 0.004f) * f + 1, alpha(MAGENTA, a * 0.25f));
            p.line(0, y, w, y, Math.max(1f, h * 0.0012f), alpha(MAGENTA, a));
        }
        // Lines converging on the vanishing point; their feet slide with the page, like the city.
        int colsN = 14;
        float spreadBottom = w * 0.17f, spreadTop = w * 0.018f;
        float foot = -shift * 0.6f;
        for (int k = -colsN; k <= colsN; k++) {
            float xTop = vpX + k * spreadTop + foot * 0.08f;
            float xBot = vpX + k * spreadBottom + foot;
            if (Math.max(xTop, xBot) < -w || Math.min(xTop, xBot) > 2 * w) continue;
            p.line(xTop, horizon, xBot, h, Math.max(1f, h * 0.0011f), alpha(CYAN, 0.55f));
        }
    }

    void drawRain(Pen p, float t) {
        int streams = 22;
        float cell = Math.max(3f, Math.min(w, h) * 0.012f);
        for (int i = 0; i < streams; i++) {
            float x = rnd(i, 21) * w;
            float speed = h * (0.10f + 0.16f * rnd(i, 22));
            float len = 4 + (int) (6 * rnd(i, 23));
            float cycle = h + len * cell * 2;
            float head = ((t * speed + rnd(i, 24) * cycle) % cycle) - len * cell;
            for (int s = 0; s < len; s++) {
                float y = head - s * cell * 1.25f;
                if (y < -cell || y > h) continue;
                float a = (s == 0 ? 0.55f : 0.30f * (1f - s / len));
                p.fillRect(x, y, cell * 0.55f, cell, alpha(GREEN, a));
            }
        }
    }

    /** A few windows switching on and off — the city is inhabited. */
    void drawFlicker(Pen p, float t, float shift) {
        int bucket = (int) Math.floor(t * 1.5f);
        for (int k = 0; k < 7; k++) {
            int i = (int) (rnd(bucket * 13 + k, 41) * count) % Math.max(1, count);
            int c = (int) (rnd(bucket * 13 + k, 42) * cols(i));
            int rIdx = (int) (rnd(bucket * 13 + k, 43) * rows(i));
            boolean on = rnd(bucket * 13 + k, 44) < 0.5f;
            float x = winX(i, c) - shift;
            if (x < -winW || x > w) continue;
            p.fillRect(x, winY(i, rIdx), winW, winH, on ? alpha(WINDOW_COLD, 0.95f) : BUILDING);
        }
    }

    /** The sign's lit text: a slow hum, and every so often a stutter, like a real tube. */
    void drawSign(Pen p, float t, float shift) {
        int tick = (int) Math.floor(t * 14f);
        boolean stutter = rnd(tick, 51) < 0.045f;
        float hum = 0.82f + 0.18f * (float) Math.sin(t * 2.3f);
        float a = stutter ? 0.12f : hum;
        float x = signX - shift;
        if (x + signW < 0 || x > w) return;
        letters(p, x, 0, alpha(CYAN, a), stutter ? 0 : h * 0.009f);
    }

    /** The stacked letters, top to bottom, inside the sign box whose left edge is at x. */
    void letters(Pen p, float x, float dy, int argb, float glow) {
        for (int i = 0; i < SIGN.length(); i++) {
            float base = signY + dy + letter * 0.35f + (i + 1) * letter * 1.12f - letter * 0.18f;
            p.text(String.valueOf(SIGN.charAt(i)), x + signW / 2f, base, letter, argb, glow);
        }
    }

    void drawPulse(Pen p, float x, float y, float age) {
        if (age < 0 || age > 1.6f) return;
        float f = age / 1.6f;
        float rx = w * 0.65f * f;
        p.ring(x, y, rx, rx * 0.32f, Math.max(2f, h * 0.003f), alpha(CYAN, 0.9f * (1f - f)));
    }

    // ------------------------------------------------------------------------------- geometry

    int cols(int i) { return Math.max(1, (int) ((bw[i] - winGapX) / (winW + winGapX))); }
    int rows(int i) { return Math.max(0, (int) ((bh[i] - winGapY * 2) / (winH + winGapY))); }
    float winX(int i, int c) { return bx[i] + winGapX + c * (winW + winGapX); }
    float winY(int i, int r) { return horizon - bh[i] + winGapY * 2 + r * (winH + winGapY); }

    /** Top of the skyline, including the sign: where the engine's skyline bitmap has to start. */
    public float skylineTop() {
        float top = signY - h * 0.02f;
        for (int i = 0; i < count; i++) top = Math.min(top, horizon - bh[i]);
        return Math.max(0f, top - h * 0.01f);
    }

    int skyAt(float y) {
        float f = Math.max(0f, Math.min(1f, y / horizon));
        return lerp(SKY_TOP, SKY_HORIZON, f);
    }

    // ------------------------------------------------------------------------------- helpers

    /** Deterministic noise in [0,1): the scene is a pure function of its seed. */
    float rnd(int i, int salt) {
        long x = seed * 0x9E3779B97F4A7C15L + i * 0xBF58476D1CE4E5B9L + salt * 0x94D049BB133111EBL;
        x ^= (x >>> 31); x *= 0x7FB5D329728EA185L; x ^= (x >>> 27); x *= 0x81DADEF4BC2DD44DL; x ^= (x >>> 33);
        return (x >>> 40) / (float) (1L << 24);
    }

    float rnd3(int a, int b, int c) { return rnd(a * 7919 + b * 104729 + c, 61); }

    public static int alpha(int argb, float a) {
        int al = Math.max(0, Math.min(255, Math.round(255 * a)));
        return (al << 24) | (argb & 0x00FFFFFF);
    }

    static int lerp(int c0, int c1, float f) {
        int a0 = (c0 >>> 24), r0 = (c0 >> 16) & 255, g0 = (c0 >> 8) & 255, b0 = c0 & 255;
        int a1 = (c1 >>> 24), r1 = (c1 >> 16) & 255, g1 = (c1 >> 8) & 255, b1 = c1 & 255;
        return ((int) (a0 + (a1 - a0) * f) << 24) | ((int) (r0 + (r1 - r0) * f) << 16)
                | ((int) (g0 + (g1 - g0) * f) << 8) | (int) (b0 + (b1 - b0) * f);
    }

    private static float[] copy(float[] a, int n) { float[] o = new float[n]; System.arraycopy(a, 0, o, 0, n); return o; }
    private static int[] copy(int[] a, int n) { int[] o = new int[n]; System.arraycopy(a, 0, o, 0, n); return o; }
}
