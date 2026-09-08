package place.poster.app.sms;

import android.app.AlertDialog;
import android.content.Context;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
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
