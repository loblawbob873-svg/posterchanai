package place.poster.app.phone;

import android.content.Context;
import android.graphics.Typeface;
import android.view.Gravity;
import android.view.View;
import android.widget.LinearLayout;
import android.widget.TextView;

import place.poster.app.ui.PcTheme;
import place.poster.app.ui.Skin;

/**
 * THE TWELVE KEYS, built in code rather than in XML.
 *
 * Not a stylistic choice: the same pad appears on the dialer and, at a different size and in a
 * different palette, on the in-call screen for a phone tree. Two XML copies of a 4x3 grid is two
 * places for a key to go missing, and neither would be caught by anything — a keypad with eleven
 * keys looks fine until somebody needs the twelfth.
 *
 * Every key is themed from the palette, so all nine of the client's themes get a keypad rather than
 * a stock grey one, and the sub-labels (ABC, DEF) are drawn at the muted colour the rest of the app
 * uses for secondary text.
 */
public final class Keypad {

    /** The keys, in reading order. `+` is what a long press on zero gives. */
    private static final String[] KEYS = { "1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#" };
    private static final String[] SUBS = { "", "ABC", "DEF", "GHI", "JKL", "MNO",
                                           "PQRS", "TUV", "WXYZ", "", "+", "" };

    public interface Press { void onKey(char digit); }

    private Keypad() { }

    /**
     * Fill `host` with the pad. `size` is the key diameter in dp — the dialer's is large, the in-call
     * one is small enough to leave the caller's name visible above it.
     */
    public static void build(final Context ctx, LinearLayout host, PcTheme.Palette pal,
                             int size, final Press press) {
        if (host == null) return;
        host.removeAllViews();
        host.setOrientation(LinearLayout.VERTICAL);
        for (int row = 0; row < 4; row++) {
            LinearLayout r = new LinearLayout(ctx);
            r.setOrientation(LinearLayout.HORIZONTAL);
            r.setGravity(Gravity.CENTER);
            for (int col = 0; col < 3; col++) {
                final int i = row * 3 + col;
                r.addView(key(ctx, pal, size, KEYS[i], SUBS[i], press));
            }
            host.addView(r, new LinearLayout.LayoutParams(
                    LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT));
        }
    }

    /**
     * Attach an extra long-press to one key after the pad is built — the dialer uses it for "1",
     * which has called voicemail since before smartphones. Done here rather than by passing another
     * callback into `build` so the in-call keypad, which must send a DTMF "1" and nothing else, is
     * unaffected by construction.
     */
    public static void onLongPress(LinearLayout host, char digit, final Runnable what) {
        if (host == null) return;
        for (int r = 0; r < host.getChildCount(); r++) {
            View row = host.getChildAt(r);
            if (!(row instanceof LinearLayout)) continue;
            LinearLayout line = (LinearLayout) row;
            for (int c = 0; c < line.getChildCount(); c++) {
                View cell = line.getChildAt(c);
                if (!(cell instanceof LinearLayout)) continue;
                View first = ((LinearLayout) cell).getChildAt(0);
                if (!(first instanceof TextView)) continue;
                if (!String.valueOf(digit).contentEquals(((TextView) first).getText())) continue;
                cell.setOnLongClickListener(new View.OnLongClickListener() {
                    @Override public boolean onLongClick(View v) {
                        if (what != null) what.run();
                        return true;
                    }
                });
            }
        }
    }

    private static View key(final Context ctx, PcTheme.Palette pal, int size,
                            final String digit, String sub, final Press press) {
        LinearLayout cell = new LinearLayout(ctx);
        cell.setOrientation(LinearLayout.VERTICAL);
        cell.setGravity(Gravity.CENTER);
        cell.setBackground(Skin.pill(ctx, pal, Skin.alpha(pal.accent, 0.10), true));
        int px = Skin.dp(ctx, size);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(px, px);
        int m = Skin.dp(ctx, 7);
        lp.setMargins(m, m, m, m);
        cell.setLayoutParams(lp);

        TextView t = new TextView(ctx);
        t.setText(digit);
        t.setTextSize(size >= 64 ? 26 : 21);
        t.setTypeface(Typeface.DEFAULT_BOLD);
        t.setTextColor(pal.text);
        t.setGravity(Gravity.CENTER);
        cell.addView(t);

        if (!sub.isEmpty()) {
            TextView s = new TextView(ctx);
            s.setText(sub);
            s.setTextSize(size >= 64 ? 9 : 8);
            s.setTextColor(pal.muted);
            s.setGravity(Gravity.CENTER);
            cell.addView(s);
        }

        cell.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                if (press != null) press.onKey(digit.charAt(0));
            }
        });
        // LONG PRESS ON ZERO IS `+`, and it is not a nicety: it is the only way to type an
        // international number on a phone keypad, and its absence is the single most-reported thing
        // missing from a hand-rolled dialer.
        if ("0".equals(digit)) {
            cell.setOnLongClickListener(new View.OnLongClickListener() {
                @Override public boolean onLongClick(View v) {
                    if (press != null) press.onKey('+');
                    return true;
                }
            });
        }
        return cell;
    }
}
