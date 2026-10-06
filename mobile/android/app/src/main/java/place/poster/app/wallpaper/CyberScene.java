package place.poster.app.wallpaper;

/**
 * PosterChan in a cyberpunk city, at night, in the rain.
 *
 * She dances on a wet rooftop in the foreground — the app's own dance frames (static/mascot/dance), her
 * reflection in the puddles at her feet — in front of a dense neon city: a hazy far skyline, nearer
 * towers with lit windows and vertical shop signs, a rooftop "POSTERCHAN" sign on the tallest one, and a
 * hologram billboard of her on another. Flying cars cross between the towers, aircraft lights blink, the
 * signs hum and now and then stutter, and rain falls over all of it. A tap on the empty home screen makes
 * her hop and sends a ripple across the roof. The palette is the app's cyberpunk theme ({@code PcTheme} /
 * client.css `--neon` / `--neon2`).
 *
 * PURE: no Android, no allocation per frame, no randomness that is not derived from a seed — the same seed
 * and the same `t` draw the same frame, which is what lets a test render it off-device and assert on pixels.
 *
 * DRAWN IN TWO HALVES, because a wallpaper is drawn thirty times a second for hours:
 *   * STATIC — {@link #drawSky} (sky, moon, smog glow, the far skyline) and {@link #drawSkyline} (the near
 *     towers, every window, the signs' boxes and unlit letters, the billboard's frame). The Android engine
 *     renders these ONCE per surface size into bitmaps and blits them.
 *   * DYNAMIC — {@link #drawFrame}: cars, blinking lights, lit sign letters, the hologram, the rooftop,
 *     PosterChan and her reflection, the rain, tap ripples, scanlines.
 *
 * The near skyline is {@link #SPAN} screens wide and slides with the home screen's page (parallax); the
 * sky and far skyline do not move, and the rooftop she stands on moves a little the other way — three
 * depths, which is what makes it read as a city and not a picture of one.
 */
public final class CyberScene {

    // The cyberpunk theme, verbatim from PcTheme's default palette.
    public static final int BG = 0xFF0A0A0F;
    public static final int CYAN = 0xFF3CE8FF;      // --neon
    public static final int MAGENTA = 0xFFFF5CF0;   // --neon2
    public static final int GREEN = 0xFF00FF88;
    public static final int GOLD = 0xFFFFCF2B;
    static final int RED = 0xFFFF3B4E;
    static final int SKY_TOP = 0xFF04030A;
    static final int SKY_MID = 0xFF0E0820;
    static final int SMOG = 0xFF3A0E3E;
    static final int FAR = 0xFF1B1131;
    static final int BUILDING = 0xFF0A0912;
    static final int BUILDING_EDGE = 0xFF19162E;
    static final int ROOF_TOP = 0xFF0D0A18;
    static final int ROOF_BOTTOM = 0xFF040307;
    static final int WINDOW_WARM = 0xFFFFC46B;
    static final int WINDOW_COLD = 0xFF7FEFFF;
    static final int WINDOW_PINK = 0xFFFF8AD8;
    static final int RAIN = 0xFFA8DCFF;

    /** How many screens wide the near skyline is: the room parallax has to move in. */
    public static final float SPAN = 1.35f;
    public static final String SIGN = "POSTERCHAN";
    /** The shop signs hung down the towers. Latin on purpose: every device has the glyphs. */
    static final String[] SHOP = {"RAMEN", "NOSTR", "BAR", "24H", "RELAY", "HOTEL", "ZAP", "ARCADE"};
    /** The dance frames ({@code pc_dance_1..8}): the engine supplies them, the scene picks one. */
    public static final int FRAMES = 8;
    /** The art's own size (static/mascot/dance/*.webp), for her proportions. */
    static final float ART_W = 406f, ART_H = 560f;

    public final float w, h, horizon, roofY;
    final long seed;
    final float unit;

    // Far skyline (static, no parallax).
    final int farN;
    final float[] fx, fw, fh;

    // Near towers, in SKYLINE coordinates (x from 0 to SPAN*w; heights above the horizon).
    final int count;
    final float[] bx, bw, bh;
    final int[] bAccent;
    final float winW, winH, winGapX, winGapY;

    // Vertical shop signs: tower, word, colour, box.
    final int shopN;
    final int[] shopTower, shopWord, shopColor;
    final float[] shopX, shopY, shopLetter;

    // The rooftop POSTERCHAN sign (skyline coordinates).
    final int signTower;
    final float signX, signY, signW, signH, letter;
    // The hologram billboard (skyline coordinates).
    final int holoTower;
    final float holoX, holoY, holoW, holoH;
    // Aircraft-warning lights on the tallest roofs.
    final int beaconN;
    final float[] beaconX, beaconY;

    // PosterChan.
    final float pcH, pcW;

    public CyberScene(float w, float h, long seed) {
        this.w = Math.max(1f, w);
        this.h = Math.max(1f, h);
        this.seed = seed;
        this.unit = Math.min(this.w, this.h);
        boolean portrait = this.h >= this.w;
        // The city's feet are hidden behind the roof she stands on: the skyline's base IS the roof line.
        this.roofY = this.h * (portrait ? 0.80f : 0.83f);
        this.horizon = this.roofY + 2f;
        // Big enough to be the subject, small enough to leave the city around her.
        pcH = Math.min(this.h * (portrait ? 0.36f : 0.52f), this.w * 0.95f * ART_H / ART_W);
        pcW = pcH * ART_W / ART_H;

        winW = Math.max(2f, unit * 0.0065f);
        winH = Math.max(2f, unit * 0.0095f);
        winGapX = winW * 0.9f;
        winGapY = winH * 0.75f;

        // ---- the far skyline: low-contrast silhouettes in the smog
        float[] xs = new float[200], ws = new float[200], hs = new float[200];
        int n = 0;
        float x = -unit * 0.02f;
        while (x < this.w && n < 200) {
            float bwid = this.w * (0.03f + 0.05f * rnd(n, 81));
            float bht = this.h * (0.10f + 0.24f * (float) Math.pow(rnd(n, 82), 1.3));
            xs[n] = x; ws[n] = bwid; hs[n] = bht;
            x += bwid * (0.55f + 0.5f * rnd(n, 83));
            n++;
        }
        farN = n; fx = copy(xs, n); fw = copy(ws, n); fh = copy(hs, n);

        // ---- the near towers
        float span = SPAN * this.w;
        xs = new float[256]; ws = new float[256]; hs = new float[256];
        int[] acc = new int[256];
        n = 0;
        x = -unit * 0.03f;
        while (x < span && n < 256) {
            float bwid = this.w * (0.07f + 0.10f * rnd(n, 1));
            float bht = this.h * (0.16f + 0.30f * (float) Math.pow(rnd(n, 2), 1.2));
            xs[n] = x; ws[n] = bwid; hs[n] = bht;
            acc[n] = rnd(n, 3) < 0.5f ? CYAN : MAGENTA;
            x += bwid + this.w * (0.004f + 0.016f * rnd(n, 4));
            n++;
        }
        count = n;
        bx = copy(xs, n); bw = copy(ws, n); bh = copy(hs, n); bAccent = copy(acc, n);

        // THE ROOFTOP SIGN sits on the tower nearest the middle of the span, raised so the sign clears
        // PosterChan's head on the middle home page: it is the first thing above her.
        float mid = span / 2f;
        signTower = nearest(mid);
        letter = Math.min(this.w * 0.074f, this.h * 0.05f);
        signW = letter * 0.62f * SIGN.length() + letter * 0.9f;
        signH = letter * 1.55f;
        float signBottomWant = roofY - pcH - this.h * 0.10f;           // well above her head
        bh[signTower] = Math.max(bh[signTower], horizon - signBottomWant - letter * 0.6f);
        bw[signTower] = Math.max(bw[signTower], signW * 0.62f);
        signX = bx[signTower] + bw[signTower] / 2f - signW / 2f;
        signY = horizon - bh[signTower] - letter * 0.6f - signH;

        // THE HOLOGRAM BILLBOARD hangs on a tower right of her, at her chest height, on the middle page.
        holoTower = nearest(mid + this.w * 0.33f);
        holoW = Math.min(this.w * 0.24f, this.h * 0.16f);
        holoH = holoW * 1.45f;
        bh[holoTower] = Math.max(bh[holoTower], horizon - (roofY - pcH * 0.95f) + holoH * 0.25f);
        bw[holoTower] = Math.max(bw[holoTower], holoW * 1.15f);
        holoX = bx[holoTower] + bw[holoTower] / 2f - holoW / 2f;
        holoY = horizon - bh[holoTower] + holoH * 0.18f;

        // Shop signs: down the side of every third tower that is tall enough, never on the two above.
        int[] st = new int[64], sw = new int[64], sc = new int[64];
        float[] sx = new float[64], sy = new float[64], sl = new float[64];
        int m = 0;
        for (int i = 0; i < count && m < 64; i++) {
            if (i == signTower || i == holoTower || rnd(i, 91) > 0.42f || bh[i] < this.h * 0.18f) continue;
            int word = (int) (rnd(i, 92) * SHOP.length) % SHOP.length;
            float lt = Math.min(bw[i] * 0.42f, this.h * 0.022f);
            float boxH = SHOP[word].length() * lt * 1.12f + lt * 0.6f;
            if (boxH > bh[i] * 0.8f) continue;
            float ssx = rnd(i, 94) < 0.5f ? bx[i] - lt * 0.55f : bx[i] + bw[i] - lt * 0.95f;
            float ssy = horizon - bh[i] + bh[i] * (0.08f + 0.25f * rnd(i, 95));
            float pad = this.w * 0.03f;
            if (ssx < holoX + holoW + pad && ssx + lt * 1.5f > holoX - pad && ssy < holoY + holoH + pad && ssy + boxH > holoY - pad) continue;
            st[m] = i; sw[m] = word; sl[m] = lt;
            sc[m] = rnd(i, 93) < 0.4f ? CYAN : rnd(i, 93) < 0.75f ? MAGENTA : GOLD;
            sx[m] = ssx;
            sy[m] = ssy;
            m++;
        }
        shopN = m; shopTower = copy(st, m); shopWord = copy(sw, m); shopColor = copy(sc, m);
        shopX = copy(sx, m); shopY = copy(sy, m); shopLetter = copy(sl, m);

        // Beacons on the five tallest roofs.
        beaconN = Math.min(5, count);
        beaconX = new float[beaconN]; beaconY = new float[beaconN];
        boolean[] used = new boolean[count];
        for (int k = 0; k < beaconN; k++) {
            int best = -1;
            for (int i = 0; i < count; i++) if (!used[i] && (best < 0 || bh[i] > bh[best])) best = i;
            used[best] = true;
            beaconX[k] = bx[best] + bw[best] * (0.3f + 0.4f * rnd(best, 96));
            beaconY[k] = horizon - bh[best] - unit * 0.035f;
        }
    }

    private int nearest(float want) {
        int best = 0; float bestD = Float.MAX_VALUE;
        for (int i = 0; i < count; i++) {
            float d = Math.abs(bx[i] + bw[i] / 2f - want);
            if (d < bestD) { bestD = d; best = i; }
        }
        return best;
    }

    // ---------------------------------------------------------------------------- static layers

    /** Sky, moon, smog and the far skyline — everything that never moves. Screen coordinates. */
    public void drawSky(Pen p) {
        p.fillRectVGrad(0, 0, w, horizon * 0.55f, SKY_TOP, SKY_MID);
        p.fillRectVGrad(0, horizon * 0.55f - 1, w, horizon * 0.45f + 1, SKY_MID, SMOG);
        p.fillRect(0, horizon, w, h - horizon, SMOG);

        // A few stars where the smog is thin.
        for (int i = 0; i < 40; i++) {
            float sx = rnd(i, 11) * w;
            float sy = (float) Math.pow(rnd(i, 12), 2.0) * horizon * 0.45f;
            float s = Math.max(1f, unit * 0.0025f);
            p.fillRect(sx, sy, s, s, ((int) (60 + 120 * rnd(i, 14)) << 24) | 0xE8EAFF);
        }

        // The moon, high and hazy.
        float mr = unit * 0.11f, mx = w * 0.78f, my = horizon * 0.16f;
        p.radialGlow(mx, my, mr * 3.2f, 0x40B9A8FF);
        p.fillCircleVGrad(mx, my, mr, 0xFFF1EEFF, 0xFFB8A9E8);
        // Smog drifting across it: soft bands, never hard stripes (hard stripes read as a synthwave sun).
        float cb = mr * 0.22f;
        p.fillRectVGrad(mx - mr * 2.2f, my + mr * 0.05f, mr * 4.4f, cb, 0x000E0820, 0x880E0820);
        p.fillRectVGrad(mx - mr * 2.2f, my + mr * 0.05f + cb, mr * 4.4f, cb, 0x880E0820, 0x000E0820);

        // The city's own light, glowing up into the smog.
        p.radialGlow(w * 0.2f, horizon, unit * 0.9f, 0x66FF2FD8);
        p.radialGlow(w * 0.85f, horizon, unit * 0.8f, 0x553CE8FF);

        // The far skyline: hazy, with the odd dim window and a spire or two.
        for (int i = 0; i < farN; i++) {
            float top = horizon - fh[i];
            p.fillRect(fx[i], top, fw[i], fh[i] + 1, FAR);
            if (rnd(i, 84) < 0.25f) p.fillRect(fx[i] + fw[i] * 0.45f, top - unit * 0.05f, Math.max(1f, unit * 0.004f), unit * 0.05f, FAR);
            for (int k = 0; k < 6; k++) {
                if (rnd3(i, k, 85) > 0.5f) continue;
                p.fillRect(fx[i] + fw[i] * rnd3(i, k, 86), top + fh[i] * rnd3(i, k, 87), winW * 0.8f, winH * 0.8f,
                        alpha(rnd3(i, k, 88) < 0.5f ? WINDOW_PINK : WINDOW_COLD, 0.35f));
            }
        }
        // Three megatowers far off, taller than anything near: the city goes on past what you can see.
        for (int k = 0; k < 3; k++) {
            float mw = w * (0.07f + 0.03f * k), mxx = w * (0.12f + 0.36f * k + 0.08f * rnd(k, 89));
            float mh = h * (0.40f + 0.12f * rnd(k, 90));
            p.fillRect(mxx, horizon - mh, mw, mh, 0xFF160E2A);
            p.fillRect(mxx + mw * 0.2f, horizon - mh - h * 0.03f, mw * 0.6f, h * 0.03f, 0xFF160E2A);
            p.fillRect(mxx + mw * 0.48f, horizon - mh - h * 0.07f, Math.max(1f, unit * 0.004f), h * 0.04f, 0xFF160E2A);
            p.line(mxx, horizon - mh, mxx + mw, horizon - mh, Math.max(1f, unit * 0.002f), alpha(k == 1 ? CYAN : MAGENTA, 0.35f));
            for (int j = 0; j < 30; j++) {
                if (rnd3(k, j, 98) > 0.5f) continue;
                p.fillRect(mxx + mw * rnd3(k, j, 99), horizon - mh * rnd3(k, j, 100), winW * 0.7f, winH * 0.7f,
                        alpha(rnd3(k, j, 101) < 0.5f ? WINDOW_PINK : WINDOW_COLD, 0.28f));
            }
        }
        // Haze over the far skyline's feet.
        p.fillRectVGrad(0, horizon - h * 0.10f, w, h * 0.10f, 0x003A0E3E, 0xB03A0E3E);
    }

    /**
     * The near towers, every window, the shop signs' boxes and unlit letters, the rooftop sign's frame and
     * the billboard's frame — offset by (dx, dy). The engine renders this ONCE into a bitmap that starts at
     * {@link #skylineTop()}, so it passes dy = -skylineTop() and blits the bitmap back at that height,
     * shifted by the parallax.
     */
    public void drawSkyline(Pen p, float dx, float dy) {
        float edge = Math.max(1f, unit * 0.0016f);
        for (int i = 0; i < count; i++) {
            float x = bx[i] + dx, top = horizon - bh[i] + dy, bot = horizon + dy + 2;
            p.fillRect(x, top, bw[i], bot - top, BUILDING);
            // Rim light down one side and along the roof, in the tower's accent: the neon that makes it a
            // cyberpunk skyline and not a bar chart.
            boolean left = rnd(i, 5) < 0.5f;
            p.line(left ? x : x + bw[i], top, left ? x : x + bw[i], bot, edge, alpha(bAccent[i], 0.55f));
            p.line(x, top, x + bw[i], top, edge * 1.4f, alpha(bAccent[i], 0.8f));
            p.line(left ? x + bw[i] : x, top, left ? x + bw[i] : x, bot, edge, BUILDING_EDGE);
            // A setback on some towers: a narrower crown on top.
            if (rnd(i, 6) < 0.35f && i != signTower && i != holoTower) {
                float cw = bw[i] * 0.55f, ch = bh[i] * 0.06f;
                p.fillRect(x + (bw[i] - cw) / 2f, top - ch, cw, ch, BUILDING);
                p.line(x + (bw[i] - cw) / 2f, top - ch, x + (bw[i] + cw) / 2f, top - ch, edge, alpha(bAccent[i], 0.6f));
            }
            // Windows: a grid, a seeded third of them lit.
            int cols = cols(i), rows = rows(i);
            for (int c = 0; c < cols; c++) {
                for (int rIdx = 0; rIdx < rows; rIdx++) {
                    if (rnd3(i, c, rIdx) > 0.32f) continue;
                    float k = rnd3(i, rIdx + 97, c);
                    int col = k < 0.6f ? WINDOW_WARM : k < 0.85f ? WINDOW_COLD : WINDOW_PINK;
                    float a = 0.30f + 0.55f * rnd3(i, c + 31, rIdx + 7);
                    p.fillRect(winX(i, c) + dx, winY(i, rIdx) + dy, winW, winH, alpha(col, a));
                }
            }
            // A band of horizontal neon across a few towers.
            if (rnd(i, 7) < 0.3f) {
                float by = top + bh[i] * (0.25f + 0.5f * rnd(i, 8));
                p.line(x, by, x + bw[i], by, edge * 2.2f, alpha(rnd(i, 9) < 0.5f ? MAGENTA : CYAN, 0.75f));
            }
        }
        // Shop signs: box + unlit letters (the lit letters hum in drawFrame).
        for (int s = 0; s < shopN; s++) {
            float lt = shopLetter[s], bwid = lt * 1.5f, bht = SHOP[shopWord[s]].length() * lt * 1.12f + lt * 0.6f;
            p.fillRect(shopX[s] + dx, shopY[s] + dy, bwid, bht, 0xF0070610);
            p.strokeRect(shopX[s] + dx, shopY[s] + dy, bwid, bht, Math.max(1f, unit * 0.002f), alpha(shopColor[s], 0.7f), 0);
            shopLetters(p, s, dx, dy, alpha(shopColor[s], 0.18f), 0);
        }
        // The rooftop sign's scaffold, box and unlit letters.
        float sb = signY + signH + dy;
        float leg = Math.max(1.5f, unit * 0.003f);
        for (int k = 0; k <= 4; k++) {
            float lx = signX + dx + signW * (0.08f + 0.84f * k / 4f);
            p.line(lx, sb, lx, horizon - bh[signTower] + dy, leg, 0xFF262236);
        }
        p.line(signX + dx + signW * 0.08f, sb + letter * 0.3f, signX + dx + signW * 0.92f, sb + letter * 0.3f, leg, 0xFF262236);
        p.fillRect(signX + dx, signY + dy, signW, signH, 0xF0080611);
        p.strokeRect(signX + dx, signY + dy, signW, signH, Math.max(2f, unit * 0.004f), alpha(MAGENTA, 0.85f), unit * 0.012f);
        p.text(SIGN, signX + dx + signW / 2f, signY + dy + signH * 0.72f, letter, alpha(MAGENTA, 0.16f), 0);
        // The billboard's frame and its dark panel; the hologram itself is drawn every frame.
        p.fillRect(holoX + dx, holoY + dy, holoW, holoH, 0xF0050812);
        p.strokeRect(holoX + dx, holoY + dy, holoW, holoH, Math.max(2f, unit * 0.003f), alpha(CYAN, 0.75f), unit * 0.008f);
    }

    // ------------------------------------------------------------------------------- the frame

    /** How far the near skyline slides for a home-screen page offset in [0, 1]. */
    public float parallaxPx(float offset) {
        float o = Math.max(0f, Math.min(1f, offset));
        return o * (SPAN - 1f) * w;
    }

    /** Which dance frame she is on at `t` — a beat of about 2.6 frames a second. */
    public int danceFrame(float t) {
        return ((int) Math.floor(Math.max(0f, t) * 2.6f)) % FRAMES;
    }

    /**
     * Everything that moves. `t` seconds since the wallpaper started, `offset` the home screen's page
     * offset in [0,1], `taps` as {x, y, tStart} triples (screen coordinates), `nTaps` of them in use.
     */
    public void drawFrame(Pen p, float t, float offset, float[] taps, int nTaps) {
        float shift = parallaxPx(offset);
        drawSearchlights(p, t, shift);
        drawCars(p, t);
        drawBeacons(p, t, shift);
        drawFlicker(p, t, shift);
        drawShopSigns(p, t, shift);
        drawSign(p, t, shift);
        drawHologram(p, t, shift);
        drawRoof(p, t, offset);
        drawRain(p, t, 0, 0.55f);                              // behind her
        drawPosterChan(p, t, offset, taps, nTaps);
        drawRain(p, t, 1, 1f);                                 // in front of her
        int nt = taps == null ? 0 : Math.min(nTaps, taps.length / 3);
        for (int i = 0; i < nt; i++) drawRipple(p, taps[i * 3], taps[i * 3 + 1], t - taps[i * 3 + 2]);
        p.scanlines(0, 0, w, h, 0x1E000000, Math.max(2f, h / 640f));
    }

    /** Two searchlights sweeping the smog from the tallest roofs. */
    void drawSearchlights(Pen p, float t, float shift) {
        for (int k = 0; k < Math.min(2, beaconN); k++) {
            float ox = beaconX[k] - shift, oy = beaconY[k] + unit * 0.03f;
            float ang = (float) (Math.PI / 2 + 0.55 * Math.sin(t * (0.21f + 0.07f * k) + k * 2.1f));
            float len = h;
            float ex = ox + (float) Math.cos(ang) * len, ey = oy - (float) Math.sin(ang) * len;
            int col = k == 0 ? CYAN : MAGENTA;
            p.line(ox, oy, ex, ey, unit * 0.09f, alpha(col, 0.035f));
            p.line(ox, oy, ex, ey, unit * 0.035f, alpha(col, 0.05f));
            p.line(ox, oy, ex, ey, unit * 0.008f, alpha(col, 0.07f));
        }
    }

    /** Flying cars crossing between the towers: a bright head, a long tail, in lanes. */
    void drawCars(Pen p, float t) {
        for (int i = 0; i < 6; i++) {
            float lane = horizon - h * (0.12f + 0.32f * rnd(i, 71));
            float speed = w * (0.06f + 0.10f * rnd(i, 72)) * (rnd(i, 73) < 0.5f ? 1f : -1f);
            float cycle = w * 1.6f;
            float pos = ((t * Math.abs(speed) + rnd(i, 74) * cycle) % cycle) - w * 0.3f;
            float x = speed > 0 ? pos : w - pos;
            float y = lane + (float) Math.sin(t * 0.7f + i) * h * 0.004f;
            float tail = w * 0.07f * (speed > 0 ? -1f : 1f);
            int head = rnd(i, 75) < 0.5f ? 0xFFFFF4D6 : CYAN;
            p.line(x + tail, y, x, y, Math.max(1.5f, unit * 0.0035f), alpha(speed > 0 ? RED : MAGENTA, 0.45f));
            p.radialGlow(x, y, unit * 0.018f, alpha(head, 0.55f));
            p.fillRect(x - unit * 0.004f, y - unit * 0.002f, unit * 0.008f, unit * 0.004f, head);
        }
    }

    void drawBeacons(Pen p, float t, float shift) {
        for (int k = 0; k < beaconN; k++) {
            float phase = (t * 0.8f + k * 0.37f) % 1f;
            if (phase > 0.18f) continue;
            float x = beaconX[k] - shift;
            if (x < -10 || x > w + 10) continue;
            p.radialGlow(x, beaconY[k], unit * 0.02f, alpha(RED, 0.8f));
            p.fillRect(x - 1.5f, beaconY[k] - 1.5f, 3, 3, RED);
        }
    }

    /** A few windows switching on and off — the city is inhabited. */
    void drawFlicker(Pen p, float t, float shift) {
        int bucket = (int) Math.floor(t * 1.5f);
        for (int k = 0; k < 7; k++) {
            int i = (int) (rnd(bucket * 13 + k, 41) * count) % Math.max(1, count);
            int r = rows(i);
            if (r == 0) continue;
            int c = (int) (rnd(bucket * 13 + k, 42) * cols(i));
            int rIdx = (int) (rnd(bucket * 13 + k, 43) * r);
            boolean on = rnd(bucket * 13 + k, 44) < 0.5f;
            float x = winX(i, c) - shift;
            if (x < -winW || x > w) continue;
            p.fillRect(x, winY(i, rIdx), winW, winH, on ? alpha(WINDOW_COLD, 0.95f) : BUILDING);
        }
    }

    void drawShopSigns(Pen p, float t, float shift) {
        for (int s = 0; s < shopN; s++) {
            float x = shopX[s] - shift;
            if (x > w || x + shopLetter[s] * 1.6f < 0) continue;
            boolean off = rnd((int) Math.floor(t * 10f) + s * 977, 52) < 0.03f;
            float a = off ? 0.15f : 0.78f + 0.2f * (float) Math.sin(t * 1.7f + s);
            shopLetters(p, s, -shift, 0, alpha(shopColor[s], a), off ? 0 : shopLetter[s] * 0.35f);
        }
    }

    void shopLetters(Pen p, int s, float dx, float dy, int argb, float glow) {
        String word = SHOP[shopWord[s]];
        float lt = shopLetter[s];
        for (int i = 0; i < word.length(); i++) {
            float base = shopY[s] + dy + lt * 0.3f + (i + 1) * lt * 1.12f - lt * 0.16f;
            p.text(String.valueOf(word.charAt(i)), shopX[s] + dx + lt * 0.75f, base, lt, argb, glow);
        }
    }

    /** The rooftop sign's lit text: a slow hum, and every so often a stutter, like a real tube. */
    void drawSign(Pen p, float t, float shift) {
        int tick = (int) Math.floor(t * 14f);
        boolean stutter = rnd(tick, 51) < 0.045f;
        float hum = 0.84f + 0.16f * (float) Math.sin(t * 2.3f);
        float x = signX - shift;
        if (x + signW < 0 || x > w) return;
        p.text(SIGN, x + signW / 2f, signY + signH * 0.72f, letter, alpha(MAGENTA, stutter ? 0.14f : hum),
                stutter ? 0 : unit * 0.016f);
    }

    /** PosterChan as a hologram ad: her dance in cyan, a scan bar rolling down, the odd glitch. */
    void drawHologram(Pen p, float t, float shift) {
        float x = holoX - shift;
        if (x + holoW < 0 || x > w) return;
        int tick = (int) Math.floor(t * 8f);
        boolean glitch = rnd(tick, 53) < 0.06f;
        float a = 0.55f + 0.12f * (float) Math.sin(t * 3.1f);
        float ih = holoH * 0.86f, iw = ih * ART_W / ART_H;
        float ix = x + (holoW - iw) / 2f, iy = holoY + holoH * 0.07f;
        int frame = ((int) Math.floor(t * 1.3f) + 3) % FRAMES;
        p.radialGlow(x + holoW / 2f, holoY + holoH / 2f, holoW * 0.8f, 0x283CE8FF);
        if (glitch) {
            p.sprite(frame, ix - holoW * 0.05f, iy, iw, ih, 0.35f, MAGENTA, false);
            p.sprite(frame, ix + holoW * 0.04f, iy, iw, ih, 0.45f, CYAN, false);
        } else {
            p.sprite(frame, ix, iy, iw, ih, a, CYAN, false);
        }
        float bar = holoY + ((t * 0.35f) % 1f) * holoH;
        p.fillRect(x, bar, holoW, Math.max(2f, holoH * 0.02f), 0x553CE8FF);
        p.scanlines(x, holoY, holoW, holoH, 0x55000000, Math.max(2f, holoH / 70f));
        p.text("POSTERCHAN", x + holoW / 2f, holoY + holoH * 0.97f, holoW * 0.11f, alpha(CYAN, 0.8f), holoW * 0.02f);
    }

    /** Where her rooftop sits horizontally: it moves a little AGAINST the page swipe (it is nearest). */
    public float roofShift(float offset) {
        float o = Math.max(0f, Math.min(1f, offset));
        return (o - 0.5f) * w * 0.10f;
    }

    /** The wet roof she dances on: a lit parapet, puddles catching the neon, a vent or two. */
    void drawRoof(Pen p, float t, float offset) {
        float rs = roofShift(offset);
        p.fillRectVGrad(0, roofY, w, h - roofY, ROOF_TOP, ROOF_BOTTOM);
        float edge = Math.max(2f, unit * 0.004f);
        p.radialGlow(w / 2f, roofY, unit * 0.5f, 0x22FF5CF0);
        p.line(0, roofY, w, roofY, edge * 2.5f, alpha(MAGENTA, 0.25f));
        p.line(0, roofY, w, roofY, edge, alpha(MAGENTA, 0.9f));
        // Vents and an AC unit as silhouettes on the roof, off to the sides.
        for (int k = 0; k < 3; k++) {
            float vx = w * (k == 0 ? 0.05f : k == 1 ? 0.80f : 0.92f) + rs * 1.3f;
            float vw = unit * (0.08f + 0.05f * k), vh = unit * (0.07f + 0.03f * (k % 2));
            p.fillRect(vx, roofY - vh, vw, vh, 0xFF07060C);
            p.line(vx, roofY - vh, vx + vw, roofY - vh, Math.max(1f, unit * 0.002f), alpha(CYAN, 0.5f));
        }
        // Puddles: the city's neon smeared into streaks on the wet surface, shimmering.
        for (int k = 0; k < 14; k++) {
            float px = w * rnd(k, 61) + rs;
            float py = roofY + (h - roofY) * (0.08f + 0.85f * rnd(k, 62));
            float len = unit * (0.04f + 0.12f * rnd(k, 63));
            float sh = 0.45f + 0.35f * (float) Math.sin(t * (1.2f + rnd(k, 64)) + k);
            int col = rnd(k, 65) < 0.5f ? MAGENTA : CYAN;
            p.line(px - len / 2f, py, px + len / 2f, py, Math.max(1.5f, unit * 0.003f), alpha(col, 0.22f * sh));
        }
    }

    /** Her feet's x on screen, for the offset. */
    public float pcCenterX(float offset) { return w * 0.5f + roofShift(offset); }

    /** PosterChan, dancing, with a rim of neon behind her and her reflection in the wet roof. */
    void drawPosterChan(Pen p, float t, float offset, float[] taps, int nTaps) {
        // A tap makes her hop (arms up): the newest tap within the last 0.7s decides.
        float hop = 0f;
        boolean tapped = false;
        int nt = taps == null ? 0 : Math.min(nTaps, taps.length / 3);
        for (int i = 0; i < nt; i++) {
            float age = t - taps[i * 3 + 2];
            if (age >= 0 && age < 0.7f) { hop = Math.max(hop, (float) Math.sin(Math.PI * age / 0.7f)); tapped = true; }
        }
        int frame = tapped ? 0 : danceFrame(t);
        float cx = pcCenterX(offset);
        float feet = roofY + pcH * 0.015f;
        float bob = (float) Math.abs(Math.sin(t * Math.PI * 1.3f)) * pcH * 0.008f;
        float y = feet - pcH - bob - hop * pcH * 0.10f;
        // The glow that separates her from the city behind.
        p.radialGlow(cx, feet - pcH * 0.55f, pcH * 0.62f, 0x44FF5CF0);
        p.radialGlow(cx, feet - pcH * 0.75f, pcH * 0.40f, 0x303CE8FF);
        // Her reflection first: upside down from her feet, faint, broken up by the scanlines/rain.
        p.sprite(frame, cx - pcW / 2f, feet, pcW, pcH, 0.22f, 0, true);
        p.sprite(frame, cx - pcW / 2f, y, pcW, pcH, 1f, 0, false);
    }

    /** The rain: thin slanted streaks. Layer 0 (behind her) is fainter and finer than layer 1. */
    void drawRain(Pen p, float t, int layer, float strength) {
        int n = layer == 0 ? 70 : 45;
        float slant = 0.18f;
        for (int i = 0; i < n; i++) {
            int k = i + layer * 1000;
            float len = h * (0.025f + 0.035f * rnd(k, 21)) * (layer == 0 ? 0.7f : 1f);
            float speed = h * (1.1f + 0.7f * rnd(k, 22));
            float cycle = h + len;
            float yy = ((t * speed + rnd(k, 23) * cycle) % cycle) - len;
            float xx = rnd(k, 24) * (w + h * slant) - yy * slant;
            float a = (0.10f + 0.22f * rnd(k, 25)) * strength;
            p.line(xx, yy, xx - len * slant, yy + len, Math.max(1f, unit * (layer == 0 ? 0.0012f : 0.0020f)), alpha(RAIN, a));
        }
    }

    /** A tap: a ripple spreading on the wet roof (flattened — it is on the ground). */
    void drawRipple(Pen p, float x, float y, float age) {
        if (age < 0 || age > 1.6f) return;
        float f = age / 1.6f;
        float ry = Math.max(y, roofY + (h - roofY) * 0.3f);
        float rx = w * 0.45f * f;
        p.ring(x, ry, rx, rx * 0.22f, Math.max(2f, unit * 0.004f), alpha(CYAN, 0.85f * (1f - f)));
        p.ring(x, ry, rx * 0.6f, rx * 0.13f, Math.max(1.5f, unit * 0.003f), alpha(MAGENTA, 0.6f * (1f - f)));
    }

    // ------------------------------------------------------------------------------- geometry

    int cols(int i) { return Math.max(1, (int) ((bw[i] - winGapX) / (winW + winGapX))); }
    int rows(int i) { return Math.max(0, (int) ((bh[i] - winGapY * 2) / (winH + winGapY))); }
    float winX(int i, int c) { return bx[i] + winGapX + c * (winW + winGapX); }
    float winY(int i, int r) { return horizon - bh[i] + winGapY * 2 + r * (winH + winGapY); }

    /** Top of the near skyline, including the rooftop sign: where the engine's skyline bitmap starts. */
    public float skylineTop() {
        float top = signY - unit * 0.03f;
        for (int i = 0; i < count; i++) top = Math.min(top, horizon - bh[i] - bh[i] * 0.06f);
        return Math.max(0f, top - h * 0.01f);
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

    private static float[] copy(float[] a, int n) { float[] o = new float[n]; System.arraycopy(a, 0, o, 0, n); return o; }
    private static int[] copy(int[] a, int n) { int[] o = new int[n]; System.arraycopy(a, 0, o, 0, n); return o; }
}
