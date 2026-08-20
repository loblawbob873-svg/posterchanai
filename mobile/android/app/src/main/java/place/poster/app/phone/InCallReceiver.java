package place.poster.app.phone;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.telecom.Call;

/**
 * Answer, reject and hang up from the notification — the buttons that matter when the screen is
 * locked, in a pocket, or on a car dashboard.
 *
 * Its actions are prefixed `TEL_`, never `CALL_`: `place.poster.app.CALL_HANGUP` is already taken by
 * the NOSTR call service, both live in the same process, and `onStartCommand` there dispatches on
 * the action without checking who sent it. A shared verb would let a cellular hang-up tear down a
 * WebRTC call, or the reverse.
 */
public class InCallReceiver extends BroadcastReceiver {

    public static final String ACTION_ANSWER = "place.poster.app.TEL_ANSWER";
    public static final String ACTION_REJECT = "place.poster.app.TEL_REJECT";
    public static final String ACTION_HANG_UP = "place.poster.app.TEL_HANGUP";

    @Override
    public void onReceive(Context ctx, Intent intent) {
        String action = intent == null ? null : intent.getAction();
        if (action == null) return;
        PcInCallService s = PcInCallService.INSTANCE;
        Call c = s == null ? null : s.primary();
        // Nothing to act on: the call ended between the notification being drawn and the button
        // being pressed, which on a lock screen is a second or two of real life. Clear the
        // notification rather than leaving one whose buttons do nothing.
        if (c == null) { InCallNotifier.clear(ctx); return; }
        if (ACTION_ANSWER.equals(action)) {
            PcInCallService.answer(c);
            try {
                ctx.startActivity(new Intent(ctx, InCallActivity.class)
                        .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP));
            } catch (Throwable ignored) { }
        } else if (ACTION_REJECT.equals(action)) {
            PcInCallService.reject(c);
        } else if (ACTION_HANG_UP.equals(action)) {
            PcInCallService.hangUp(c);
        }
    }
}
