package place.poster.app.phone;

import android.content.Context;
import android.graphics.Typeface;
import android.view.Gravity;
import android.view.MotionEvent;
import android.view.View;
import android.widget.LinearLayout;
import android.widget.TextView;

import place.poster.app.ui.PcTheme;
import place.poster.app.ui.Skin;

/**
 * THE TWELVE KEYS, built in code rather than in XML.
 *
 * Not a stylistic choice: the same pad appears full-screen on the dialer's Keypad tab and small on
 * the in-call screen for a phone tree. Two XML copies of a 4x3 grid is two places for a key to go
 * missing, and neither would be caught by anything — a keypad with eleven keys looks fine until
 * somebody needs the twelfth.
 *
 * EVERY KEY LIGHTS UP WHEN PRESSED (KeyGlow). The dialpad is the surface people judge a phone by,
 * and a press that produces nothing but a grey ripple is what makes a hand-rolled dialer feel cheap.
 * The digit lights with it on a theme that glows, and both degrade to a flat colour change on the
 * light palettes, where a halo behind dark text destroys it.
 */
public final class Keypad {

    /** The keys, in reading order. `+` is what a long press on zero gives. */
    private static final String[] KEYS = { "1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#" };
    private static final String[] SUBS = { "", "ABC", "DEF", "GHI", "JKL", "MNO",
                                           "PQRS", "TUV", "WXYZ", "", "+", "" };

    public interface Press { void onKey(char digit); }

    private Keypad() { }

    /**
     * Fill `host` with the pad. `size` is the key diameter in dp — the dialer's tab is large enough
     * to be hit without looking, the in-call one small enough to leave the caller's name visible.
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

    private static View key(final Context ctx, final PcTheme.Palette pal, int size,
                            final String digit, String sub, final Press press) {
        final LinearLayout cell = new LinearLayout(ctx);
        cell.setOrientation(LinearLayout.VERTICAL);
        cell.setGravity(Gravity.CENTER);
        final KeyGlow glow = new KeyGlow(ctx, pal);
        cell.setBackground(glow);
        // A background only ever sees a press if the view says it is clickable — without this the
        // Drawable's state never changes and the key never lights, with nothing to say why.
        cell.setClickable(true);
        int px = Skin.dp(ctx, size);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(px, px);
        int m = Skin.dp(ctx, size >= 70 ? 9 : 7);
        lp.setMargins(m, m, m, m);
        cell.setLayoutParams(lp);

        final TextView t = new TextView(ctx);
        t.setText(digit);
        t.setTextSize(size >= 70 ? 30 : (size >= 60 ? 26 : 21));
        t.setTypeface(Typeface.DEFAULT_BOLD);
        t.setTextColor(pal.text);
        t.setGravity(Gravity.CENTER);
        cell.addView(t);

        TextView s = null;
        if (!sub.isEmpty()) {
            s = new TextView(ctx);
            s.setText(sub);
            s.setTextSize(size >= 70 ? 10 : (size >= 60 ? 9 : 8));
            s.setTextColor(pal.muted);
            s.setGravity(Gravity.CENTER);
            try { s.setLetterSpacing(0.12f); } catch (Throwable ignored) { }
            cell.addView(s);
        }

        // THE DIGIT LIGHTS WITH THE KEY. The background redraws itself on a state change; a TextView
        // does not, so the press is observed here and the colour set directly. Touch rather than
        // click, because the light has to arrive on the way DOWN — a glow that appears when you let
        // go is a glow nobody sees.
        final TextView sub2 = s;
        cell.setOnTouchListener(new View.OnTouchListener() {
            @Override public boolean onTouch(View v, MotionEvent e) {
                switch (e.getActionMasked()) {
                    case MotionEvent.ACTION_DOWN:
                        t.setTextColor(pal.neon ? pal.accent : pal.onAccent());
                        if (pal.neon) t.setShadowLayer(Skin.dp(ctx, 12), 0, 0, pal.accent);
                        if (sub2 != null) sub2.setTextColor(pal.neon ? pal.accent : pal.onAccent());
                        break;
                    case MotionEvent.ACTION_UP:
                    case MotionEvent.ACTION_CANCEL:
                        t.setTextColor(pal.text);
                        t.setShadowLayer(0, 0, 0, 0);
                        if (sub2 != null) sub2.setTextColor(pal.muted);
                        break;
                }
                return false;                // never consume: the click still has to happen
            }
        });

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
