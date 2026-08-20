package place.poster.app.ui;

/**
 * THE CLIENT'S NINE THEMES, IN JAVA, so the native screens are not a different app.
 *
 * The launcher, the dialer and the SMS app are drawn by Android rather than by the WebView — that is
 * the whole safety argument for the launcher (see HomeActivity) and the reason a text still arrives
 * when the renderer is dead. The cost of that decision is that they inherit NOTHING from
 * static/css/client.css: no variables, no glow, no palette. Somebody who set the app to Cherry
 * Blossom and then opened their messages would find a stock grey Android list.
 *
 * So the palettes are transcribed here, value for value, from the `:root[data-theme=…]` blocks in
 * client.css, and the WebView writes the chosen slug into SharedPreferences (PcThemePlugin) whenever
 * it changes. tests/test_android_theme_palettes.py reads BOTH files and fails when they drift —
 * which is the only thing that can keep two copies of a palette honest.
 *
 * PURE. No Android imports at all: the table and its lookup are ordinary Java so they can be run,
 * not grepped. `Skin` is what turns a Palette into drawables, and `PcThemeStore` is what remembers
 * the slug.
 */
public final class PcTheme {

    /** Cyberpunk is the flagship and the fallback: an unknown slug is this, never a stock grey. */
    public static final String DEFAULT_SLUG = "cyberpunk";

    /** Every slug the client offers, in the order it offers them. */
    public static final String[] SLUGS = {
        "cyberpunk", "cherryblossom", "professional", "win98", "winxp",
        "animegirl", "sovietgothic", "dark", "monero"
    };

    public static final class Palette {
        public final String slug;
        /** Page background, and the second stop of its vertical wash. */
        public final int bg, bg2;
        /** Card / bar surfaces. Both carry alpha, exactly as the CSS does. */
        public final int panel, panel2;
        public final int text, muted, line;
        /** --neon and --neon2. The primary and secondary accents. */
        public final int accent, accent2;
        public final int green, amber, danger;
        /** The two ambient corner washes (--amb1 top centre, --amb2 bottom right). */
        public final int amb1, amb2;
        /** --r, the corner radius, in dp. win98 is 0 and must STAY 0 — square is the theme. */
        public final int radiusDp;
        /**
         * Whether this theme glows. The CSS says it by setting --glow:none, and it is not decoration:
         * a neon halo behind text on a LIGHT background is the readability bug the stylesheet's own
         * `:root[data-theme] *{text-shadow:none}` rule exists to prevent.
         */
        public final boolean neon;
        /** Cyberpunk only: the scanlines and the grid. `:root[data-theme] .scanlines{display:none}`. */
        public final boolean decor;

        Palette(String slug, int bg, int bg2, int panel, int panel2, int text, int muted, int line,
                int accent, int accent2, int green, int amber, int danger,
                int amb1, int amb2, int radiusDp, boolean neon, boolean decor) {
            this.slug = slug; this.bg = bg; this.bg2 = bg2; this.panel = panel; this.panel2 = panel2;
            this.text = text; this.muted = muted; this.line = line;
            this.accent = accent; this.accent2 = accent2;
            this.green = green; this.amber = amber; this.danger = danger;
            this.amb1 = amb1; this.amb2 = amb2; this.radiusDp = radiusDp;
            this.neon = neon; this.decor = decor;
        }

        /** True when the background is dark enough that white-ish text is the right default. */
        public boolean isDark() { return luminance(bg) < 0.5; }

        /** Text that must sit ON the accent (a call button's label, a send icon). */
        public int onAccent() { return luminance(accent) < 0.55 ? 0xFFFFFFFF : 0xFF101014; }
    }

    private PcTheme() { }

    /** Relative luminance, 0..1, ignoring alpha. Plain sRGB weights — good enough to pick a text colour. */
    public static double luminance(int argb) {
        double r = ((argb >> 16) & 0xFF) / 255.0;
        double g = ((argb >> 8) & 0xFF) / 255.0;
        double b = (argb & 0xFF) / 255.0;
        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
    }

    /** rgba(r,g,b,a) as the CSS writes it → an ARGB int. Kept so the table below reads like the CSS. */
    static int rgba(int r, int g, int b, double a) {
        int al = (int) Math.round(Math.max(0, Math.min(1, a)) * 255.0);
        return (al << 24) | (r << 16) | (g << 8) | b;
    }

    /**
     * The palette for a slug. An unknown, empty or null slug is cyberpunk — never a stock theme,
     * because "the theme did not load" and "the user chose grey" must not look the same.
     */
    public static Palette of(String slug) {
        String s = slug == null ? "" : slug.trim().toLowerCase(java.util.Locale.ROOT);
        if ("cherryblossom".equals(s)) return new Palette(s,
            0xFFFFF5F8, 0xFFFFE9F0, rgba(255,255,255,.74), rgba(255,231,240,.90),
            0xFF4A2B38, 0xFF7A5260, rgba(255,111,165,.30),
            0xFFFF6FA5, 0xFFC77DFF, 0xFF3FAE7E, 0xFFE08A2E, 0xFFD61F4E,
            rgba(255,143,176,.16), rgba(199,125,255,.10), 16, false, false);
        if ("professional".equals(s)) return new Palette(s,
            0xFFF5F7FA, 0xFFEAEEF3, rgba(255,255,255,.92), 0xFFFFFFFF,
            0xFF1F2937, 0xFF6B7280, rgba(15,23,42,.12),
            0xFF2563EB, 0xFF4F46E5, 0xFF16A34A, 0xFFD97706, 0xFFDC2645,
            rgba(37,99,235,.07), rgba(99,102,241,.06), 10, false, false);
        if ("win98".equals(s)) return new Palette(s,
            0xFFC0C0C0, 0xFFB8B8B0, 0xFFC0C0C0, 0xFFD4D0C8,
            0xFF000000, 0xFF404040, 0xFF808080,
            0xFF000080, 0xFF1084D0, 0xFF008000, 0xFF808000, 0xFFC00000,
            0x00000000, 0x00000000, 0, false, false);
        if ("winxp".equals(s)) return new Palette(s,
            0xFFEAF1FB, 0xFFD6E4F7, rgba(255,255,255,.93), 0xFFECE9D8,
            0xFF0A1A3A, 0xFF5A6A8A, rgba(10,91,196,.30),
            0xFF0A5BC4, 0xFF2F8F2F, 0xFF2F8F2F, 0xFFF0A000, 0xFFC81E3A,
            rgba(255,255,255,.10), rgba(10,91,196,.06), 8, false, false);
        if ("animegirl".equals(s)) return new Palette(s,
            0xFFFBF0FF, 0xFFF3E3FF, rgba(255,255,255,.78), rgba(247,227,255,.90),
            0xFF5A3A6E, 0xFFA888BD, rgba(157,123,255,.30),
            0xFFFF7EC8, 0xFF9D7BFF, 0xFF3FBF9C, 0xFFF0A43A, 0xFFD61F4E,
            rgba(255,126,200,.16), rgba(157,123,255,.12), 18, false, false);
        if ("sovietgothic".equals(s)) return new Palette(s,
            0xFF17110F, 0xFF211713, rgba(34,24,20,.82), rgba(46,32,27,.92),
            0xFFE7D8BE, 0xFFA08B6F, rgba(184,30,34,.34),
            // --danger: Soviet Gothic declares none, so it INHERITS the flagship's #ff6b8b. Written
            // out rather than "improved" to the theme's own red: the CSS is what the app shows, and a
            // native screen that picked a nicer colour would simply be a second palette.
            0xFFB81E22, 0xFF7A0D10, 0xFF6B7F3A, 0xFFC79A3A, 0xFFFF6B8B,
            rgba(184,30,34,.10), rgba(199,154,58,.05), 3, true, false);
        if ("dark".equals(s)) return new Palette(s,
            0xFF17181C, 0xFF202229, rgba(38,40,46,.82), 0xFF2A2C33,
            0xFFE6E7EA, 0xFF9AA0AA, rgba(255,255,255,.13),
            0xFF4493F8, 0xFF7C5CFF, 0xFF3FB950, 0xFFD29922, 0xFFFF6B8B,
            0x00000000, 0x00000000, 12, false, false);
        if ("monero".equals(s)) return new Palette(s,
            0xFF0C0B0E, 0xFF151217, rgba(28,22,20,.78), rgba(41,31,27,.92),
            0xFFF4EDE7, 0xFFA2968C, rgba(255,102,0,.30),
            0xFFFF6600, 0xFFFF8C42, 0xFF4FB783, 0xFFFFAB40, 0xFFFF5A7A,
            rgba(255,102,0,.09), rgba(255,140,66,.05), 14, true, false);
        // cyberpunk, and everything unrecognised.
        return new Palette(DEFAULT_SLUG,
            0xFF0A0A0F, 0xFF12121A, rgba(18,18,28,.72), rgba(26,26,42,.85),
            0xFFEDEEFA, 0xFF9FA1C6, rgba(125,210,255,.16),
            0xFF3CE8FF, 0xFFFF5CF0, 0xFF00FF88, 0xFFFFCF2B, 0xFFFF6B8B,
            rgba(0,255,255,.06), rgba(255,0,255,.045), 14, true, true);
    }

    public static boolean known(String slug) {
        for (String s : SLUGS) if (s.equals(slug)) return true;
        return false;
    }
}
