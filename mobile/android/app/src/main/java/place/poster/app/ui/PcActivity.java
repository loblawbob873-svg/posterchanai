package place.poster.app.ui;

import android.app.Activity;
import android.graphics.drawable.Drawable;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.widget.ImageView;
import android.widget.TextView;
import android.widget.Toast;

/**
 * WHAT EVERY NATIVE POSTERCHAN SCREEN SHARES: the person's theme, and nothing else.
 *
 * A plain android.app.Activity — no AppCompat delegate, no Capacitor bridge, no WebView. These
 * screens exist BECAUSE the WebView can die (the launcher must survive it; a text message must
 * arrive whatever state the renderer is in), so depending on any of that would undo the reason they
 * are native. What they must not be is a stock grey Android list beside a Cherry Blossom app, hence
 * this: one place that reads the mirrored theme and turns it into pixels.
 *
 * Re-read in onStart, never cached across a stop: the theme is changed inside the app and these
 * screens are what you come back to afterwards.
 */
public abstract class PcActivity extends Activity {

    protected PcTheme.Palette pal;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        pal = PcThemeStore.palette(this);
    }

    @Override
    protected void onStart() {
        super.onStart();
        PcTheme.Palette now = PcThemeStore.palette(this);
        boolean changed = pal == null || !pal.slug.equals(now.slug);
        pal = now;
        if (changed) onThemeChanged();
    }

    /** Repaint everything the palette touches. Called when the theme changed while we were away. */
    protected void onThemeChanged() { }

    /** The page background: the theme's wash, its two ambient corner glows, and its decor. */
    protected void paintPage(int rootId) {
        View v = findViewById(rootId);
        if (v != null) v.setBackground(Skin.page(pal));
        // The system bars take the page's own colour so the screen reads as one surface rather than
        // as a themed rectangle inside somebody else's chrome.
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
            try {
                getWindow().setStatusBarColor(pal.bg);
                getWindow().setNavigationBarColor(pal.bg);
            } catch (Throwable ignored) { }
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) {
            try {
                // Dark icons on a light theme. Without this every light palette gets white status
                // icons on a white bar — invisible, and it looks like the bar is empty.
                View decor = getWindow().getDecorView();
                int flags = decor.getSystemUiVisibility();
                if (pal.isDark()) flags &= ~View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
                else flags |= View.SYSTEM_UI_FLAG_LIGHT_STATUS_BAR;
                decor.setSystemUiVisibility(flags);
            } catch (Throwable ignored) { }
        }
    }

    /** A sprite icon in a palette colour. One drawable, nine themes — see scripts/gen_android_icons.py. */
    protected Drawable tint(int res, int color) { return Skin.icon(this, res, color); }

    protected void icon(int viewId, int res, int color) {
        View v = findViewById(viewId);
        if (v instanceof ImageView) ((ImageView) v).setImageDrawable(tint(res, color));
    }

    protected void text(int viewId, int color) {
        View v = findViewById(viewId);
        if (v instanceof TextView) ((TextView) v).setTextColor(color);
    }

    protected int dp(float v) { return Skin.dp(this, v); }

    protected void say(String s) {
        try { Toast.makeText(this, s, Toast.LENGTH_SHORT).show(); } catch (Throwable ignored) { }
    }

    /** Initials for an avatar, from a contact name or a bare number. */
    public static String initials(String label) {
        if (label == null) return "?";
        String s = label.trim();
        if (s.isEmpty()) return "?";
        /* A NUMBER HAS NO INITIALS WORTH TAKING; its last two digits are what tells two apart at a
         * glance in a list, which is the job.
         *
         * "Contains no letters", not "is mostly digits". The first version compared the digit count
         * against the string length minus three, which said yes to `5550104477` and NO to
         * `+1 555 010 4477` — the same number with the spaces a phone book puts in — so one of them
         * got a circle reading "+1". Letters are the actual question. */
        if (!s.matches(".*\\p{L}.*")) {
            String d = s.replaceAll("[^0-9]", "");
            if (d.isEmpty()) return "#";
            return d.length() >= 2 ? d.substring(d.length() - 2) : d;
        }
        String[] parts = s.split("\\s+");
        if (parts.length >= 2 && !parts[1].isEmpty()) {
            return ("" + parts[0].charAt(0) + parts[1].charAt(0)).toUpperCase(java.util.Locale.ROOT);
        }
        return s.substring(0, 1).toUpperCase(java.util.Locale.ROOT);
    }
}
