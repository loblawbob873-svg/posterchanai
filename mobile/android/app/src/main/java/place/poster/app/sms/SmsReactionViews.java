package place.poster.app.sms;

import android.app.AlertDialog;
import android.content.Context;
import android.graphics.drawable.GradientDrawable;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.PopupWindow;
import android.widget.TextView;
import java.util.*;
import place.poster.app.R;
import place.poster.app.ui.PcTheme;
import place.poster.app.ui.Skin;

/** Native reaction controls. Provider mutations remain in the thread's guarded send handler. */
final class SmsReactionViews {
    interface Action { void send(String kind, boolean remove); }
    interface Details { void show(String originalText); }
    private static final String[] KINDS = {"heart", "like", "dislike", "laugh", "emphasize", "question"};
    private static final int[] LABELS = {R.string.sms_reaction_heart, R.string.sms_reaction_like,
            R.string.sms_reaction_dislike, R.string.sms_reaction_laugh,
            R.string.sms_reaction_emphasize, R.string.sms_reaction_question};

    static AlertDialog picker(Context context, SmsReactionThread history, SmsMsg target, Action action) {
        if (history == null || !history.canReact(target)) return null;
        SmsReactions.Chip own = history.own(target);
        List<String> labels = new ArrayList<>();
        for (int i = 0; i < KINDS.length; i++) {
            SmsReactions.Parsed p = SmsReactions.parse(SmsReactions.format(KINDS[i], false, target.body));
            labels.add(p.emoji + "  " + context.getString(LABELS[i]));
        }
        if (own != null) labels.add(context.getString(R.string.sms_reaction_remove));
        // One dialog choice consumes this picker even if two touch events were already queued.
        final boolean[] chosen = {false};
        return new AlertDialog.Builder(context).setTitle(R.string.sms_react)
                .setItems(labels.toArray(new String[0]), (dialog, which) -> {
                    if (chosen[0]) return;
                    chosen[0] = true;
                    if (which < KINDS.length) action.send(KINDS[which], false);
                    else if (own != null) action.send(own.kind, true);
                }).create();
    }

    /* THE PHONE-STYLE BAR ("text messages need that reaction thing that Android and iphone do when
     * long-pressing on the message"). Long-press a message and the six reactions sit in a row right
     * over it -- your current one highlighted, tap it again to take it back -- with ⋯ for the rest of
     * the message menu. Same sends as the picker above (the guarded handler in ThreadActivity). */
    static LinearLayout barView(Context context, SmsReactionThread history, SmsMsg target,
                                PcTheme.Palette palette, Action action, Runnable more, Runnable done) {
        if (history == null || !history.canReact(target)) return null;
        final SmsReactions.Chip own = history.own(target);
        final LinearLayout row = new LinearLayout(context);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        int pad = Skin.dp(context, 6);
        row.setPadding(pad, pad, pad, pad);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(Skin.opaque(palette.panel, palette.bg));
        bg.setStroke(Math.max(1, Skin.dp(context, 1)), palette.line);
        bg.setCornerRadius(Skin.dp(context, 999));
        row.setBackground(bg);
        row.setElevation(Skin.dp(context, 8));
        // One choice consumes the bar even if two taps were already queued.
        final boolean[] chosen = {false};
        int size = Skin.dp(context, 44);
        for (int i = 0; i < KINDS.length; i++) {
            final String kind = KINDS[i];
            SmsReactions.Parsed p = SmsReactions.parse(SmsReactions.format(kind, false, target.body));
            TextView b = new TextView(context);
            b.setText(p.emoji);
            b.setTextSize(24);
            b.setGravity(Gravity.CENTER);
            b.setContentDescription(context.getString(LABELS[i]));
            final boolean mine = own != null && kind.equals(own.kind);
            if (mine) {
                GradientDrawable on = new GradientDrawable();
                on.setColor(Skin.alpha(palette.accent, 0.28));
                on.setCornerRadius(Skin.dp(context, 999));
                b.setBackground(on);
                b.setSelected(true);
            }
            b.setOnClickListener(v -> {
                if (chosen[0]) return;
                chosen[0] = true;
                if (done != null) done.run();
                action.send(kind, mine);
            });
            row.addView(b, new LinearLayout.LayoutParams(size, size));
        }
        if (more != null) {
            TextView dots = new TextView(context);
            dots.setText("⋯");
            dots.setTextSize(22);
            dots.setTextColor(palette.text);
            dots.setGravity(Gravity.CENTER);
            dots.setContentDescription(context.getString(R.string.sms_more_actions));
            dots.setOnClickListener(v -> {
                if (chosen[0]) return;
                chosen[0] = true;
                if (done != null) done.run();
                more.run();
            });
            row.addView(dots, new LinearLayout.LayoutParams(size, size));
        }
        return row;
    }

    /** Show the bar over `anchor` (under it when there is no room above). Null when not reactable. */
    static PopupWindow bar(View anchor, SmsReactionThread history, SmsMsg target, PcTheme.Palette palette,
                           Action action, Runnable more) {
        final PopupWindow[] pw = {null};
        LinearLayout row = barView(anchor.getContext(), history, target, palette, action, more,
                () -> { if (pw[0] != null) pw[0].dismiss(); });
        if (row == null) return null;
        row.measure(View.MeasureSpec.UNSPECIFIED, View.MeasureSpec.UNSPECIFIED);
        PopupWindow popup = new PopupWindow(row, ViewGroup.LayoutParams.WRAP_CONTENT,
                ViewGroup.LayoutParams.WRAP_CONTENT, true);
        popup.setOutsideTouchable(true);
        popup.setElevation(Skin.dp(anchor.getContext(), 8));
        pw[0] = popup;
        int[] at = new int[2];
        anchor.getLocationOnScreen(at);
        int h = row.getMeasuredHeight(), w = row.getMeasuredWidth(), gap = Skin.dp(anchor.getContext(), 4);
        int screen = anchor.getResources().getDisplayMetrics().widthPixels;
        int x = Math.max(gap, Math.min(at[0] + (anchor.getWidth() - w) / 2, screen - w - gap));
        int y = at[1] - h - gap;
        if (y < Skin.dp(anchor.getContext(), 24)) y = at[1] + anchor.getHeight() + gap;
        popup.showAtLocation(anchor, Gravity.TOP | Gravity.START, x, y);
        return popup;
    }

    static void bind(LinearLayout host, SmsReactionThread history, SmsMsg target,
                     PcTheme.Palette palette, Details details) {
        host.removeAllViews();
        List<SmsReactions.Chip> chips = history == null ? null
                : history.projection.chipsByTarget.get(SmsReactionThread.id(target));
        host.setVisibility(chips == null || chips.isEmpty() ? View.GONE : View.VISIBLE);
        if (chips == null) return;
        Context context = host.getContext();
        for (SmsReactions.Chip chip : chips) {
            Button badge = new Button(context);
            badge.setText(chip.emoji + ("self".equals(chip.actor)
                    ? " · " + context.getString(R.string.sms_reaction_you) : ""));
            badge.setContentDescription(badge.getText());
            badge.setTextColor(palette.text);
            badge.setTextSize(14);
            badge.setAllCaps(false);
            badge.setBackground(Skin.ghost(context, palette, palette.accent, false));
            badge.setOnClickListener(view -> {
                SmsMsg source = history.find(chip.sourceId);
                if (source != null) details.show(source.body);
            });
            host.addView(badge);
        }
    }
}
