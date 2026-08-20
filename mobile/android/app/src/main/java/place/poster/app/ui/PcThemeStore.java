package place.poster.app.ui;

import android.content.Context;

/**
 * WHICH THEME THE PERSON PICKED, remembered where a plain Activity can read it.
 *
 * The client keeps `pc_theme` in localStorage, which lives inside the WebView and is reachable only
 * from JavaScript. The launcher, the dialer and the SMS screens have no WebView by design — that is
 * what makes them survive a dead renderer — so the choice is mirrored into SharedPreferences by
 * PcThemePlugin every time it changes, and read from here.
 *
 * A MIRROR, NEVER A SECOND SOURCE OF TRUTH. localStorage stays authoritative; this copy is only ever
 * written from it. A missing copy means "the app has not been opened since this was added", and the
 * answer to that is the flagship theme, which is also what the client shows by default — so the two
 * halves agree even before they have ever spoken.
 */
public final class PcThemeStore {

    private static final String FILE = "pc_ui";
    private static final String KEY = "theme";

    private PcThemeStore() { }

    public static String slug(Context ctx) {
        try {
            String s = ctx.getApplicationContext()
                    .getSharedPreferences(FILE, Context.MODE_PRIVATE).getString(KEY, null);
            return PcTheme.known(s) ? s : PcTheme.DEFAULT_SLUG;
        } catch (Throwable t) { return PcTheme.DEFAULT_SLUG; }
    }

    public static PcTheme.Palette palette(Context ctx) { return PcTheme.of(slug(ctx)); }

    /** Written only by PcThemePlugin, from the client's own theme setter. Unknown slugs are ignored. */
    public static void remember(Context ctx, String slug) {
        if (!PcTheme.known(slug)) return;
        try {
            ctx.getApplicationContext().getSharedPreferences(FILE, Context.MODE_PRIVATE)
               .edit().putString(KEY, slug).apply();
        } catch (Throwable ignored) { }
    }
}
