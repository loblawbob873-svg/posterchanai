package place.poster.app.phone;

import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.telecom.Call;
import android.view.View;
import android.view.WindowManager;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.TextView;

import place.poster.app.R;
import place.poster.app.sms.PhoneBook;
import place.poster.app.ui.CalendarPeek;
import place.poster.app.ui.PcActivity;
import place.poster.app.ui.Skin;

/**
 * THE CALL SCREEN.
 *
 * It draws whatever PcInCallService is holding and asks CallRules what it is allowed to offer. Every
 * control is HIDDEN rather than disabled when it is illegal: the platform answers an impossible
 * request by doing nothing at all — no exception, no callback — so a greyed button and a live one
 * that silently fails are indistinguishable to the person pressing it.
 *
 * IT SHOWS OVER THE LOCK SCREEN AND TURNS IT ON. Both flags, and on API 27+ the methods that
 * replaced them, because a ringing phone whose screen stays dark is a missed call. The window also
 * keeps the screen awake for the length of the call, which is the one place in this whole feature a
 * wake lock would otherwise be reached for — the window flag is scoped to the activity and released
 * with it, where a PowerManager lock survives whatever forgets to release it.
 *
 * NO POLLING except the call timer, which counts seconds because that is literally what it displays.
 * Everything else is pushed by the service.
 */
public class InCallActivity extends PcActivity {

    private TextView name, number, status, avatar, context;
    private ImageView answer, end, mute, speaker, keypadBtn, hold;
    private LinearLayout pad, controls;
    private final Handler main = new Handler(Looper.getMainLooper());
    private boolean padOpen = false;
    private String shownFor = "";

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);
        showOverLockScreen();
        setContentView(R.layout.tel_incall);

        name = (TextView) findViewById(R.id.pc_ic_name);
        number = (TextView) findViewById(R.id.pc_ic_number);
        status = (TextView) findViewById(R.id.pc_ic_status);
        avatar = (TextView) findViewById(R.id.pc_ic_avatar);
        context = (TextView) findViewById(R.id.pc_ic_context);
        answer = (ImageView) findViewById(R.id.pc_ic_answer);
        end = (ImageView) findViewById(R.id.pc_ic_end);
        mute = (ImageView) findViewById(R.id.pc_ic_mute);
        speaker = (ImageView) findViewById(R.id.pc_ic_speaker);
        keypadBtn = (ImageView) findViewById(R.id.pc_ic_keypad);
        hold = (ImageView) findViewById(R.id.pc_ic_hold);
        pad = (LinearLayout) findViewById(R.id.pc_ic_pad);
        controls = (LinearLayout) findViewById(R.id.pc_ic_controls);

        answer.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { PcInCallService.answer(call()); draw(); }
        });
        end.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                Call c = call();
                // REJECT AND HANG UP ARE DIFFERENT ACTIONS. Rejecting a ringing call can send the
                // caller to voicemail; disconnecting one just drops it. Same red button, two
                // meanings, chosen by the state.
                if (CallRules.canReject(PcInCallService.stateOf(c))) PcInCallService.reject(c);
                else PcInCallService.hangUp(c);
                draw();
            }
        });
        mute.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                PcInCallService s = PcInCallService.INSTANCE;
                if (s != null) s.toggleMute();
                draw();
            }
        });
        speaker.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                PcInCallService s = PcInCallService.INSTANCE;
                if (s != null) s.setSpeaker(!s.speaker());
                draw();
            }
        });
        hold.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) {
                Call c = call();
                PcInCallService.hold(c, PcInCallService.stateOf(c) == CallRules.STATE_ACTIVE);
                draw();
            }
        });
        keypadBtn.setOnClickListener(new View.OnClickListener() {
            @Override public void onClick(View v) { padOpen = !padOpen; draw(); }
        });

        draw();
    }

    private void showOverLockScreen() {
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
                setShowWhenLocked(true);
                setTurnScreenOn(true);
            }
            // The flags are still set as well, and deliberately: on several OEM builds the methods
            // above are honoured only in combination with them, and a ringing phone with a dark
            // screen is a missed call.
            getWindow().addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED
                    | WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON
                    | WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON
                    | WindowManager.LayoutParams.FLAG_DISMISS_KEYGUARD);
        } catch (Throwable ignored) { }
    }

    @Override
    protected void onStart() {
        super.onStart();
        PcInCallService.setWatcher(new PcInCallService.Watcher() {
            @Override public void onCallsChanged() {
                main.post(new Runnable() { @Override public void run() { draw(); } });
            }
        });
        draw();
        main.post(tick);
    }

    @Override
    protected void onStop() {
        super.onStop();
        PcInCallService.setWatcher(null);
        main.removeCallbacks(tick);
    }

    /** THE ONLY REPEATING THING IN THIS FEATURE, and it repeats because it displays seconds. */
    private final Runnable tick = new Runnable() {
        @Override public void run() {
            drawStatus();
            main.postDelayed(this, 1000);
        }
    };

    /** BACK MUST NOT END A CALL. It leaves the screen; the call carries on with its notification. */
    @Override
    public void onBackPressed() { moveTaskToBack(true); }

    private Call call() {
        PcInCallService s = PcInCallService.INSTANCE;
        return s == null ? null : s.primary();
    }

    private void draw() {
        Call c = call();
        if (c == null) { finish(); return; }
        int state = PcInCallService.stateOf(c);
        if (CallRules.isOver(state)) { finish(); return; }

        paintPage(R.id.pc_ic_root);
        String num = PcInCallService.numberOf(c);
        String who = PhoneBook.label(this, num);
        if (who.isEmpty()) who = getString(R.string.tel_unknown);
        name.setText(who);
        name.setTextColor(pal.text);
        Skin.glow(name, pal);
        number.setText(who.equals(num) ? "" : Dial.pretty(num));
        number.setTextColor(pal.muted);
        avatar.setText(initials(who));
        avatar.setBackground(Skin.avatar(this, pal, who));
        context.setBackground(Skin.ghost(this, pal, pal.accent2, false));
        context.setTextColor(pal.text);
        Skin.heading(status, pal);
        status.setTextColor(pal.muted);
        status.setTextSize(14);

        boolean ringing = CallRules.canAnswer(state);
        answer.setVisibility(ringing ? View.VISIBLE : View.GONE);
        answer.setImageDrawable(tint(R.drawable.ic_pc_call, 0xFF0B1A10));
        answer.setBackground(Skin.pill(this, pal, pal.green, true));
        end.setImageDrawable(tint(R.drawable.ic_pc_close, 0xFFFFFFFF));
        end.setBackground(Skin.pill(this, pal, pal.danger, true));

        // Hidden, never greyed: the platform answers an impossible request by doing nothing, so a
        // disabled-looking button and a broken one are the same thing to whoever presses it.
        boolean canRoute = CallRules.canRoute(state);
        controls.setVisibility(canRoute && !ringing ? View.VISIBLE : View.GONE);
        keypadBtn.setVisibility(CallRules.canSendTones(state) ? View.VISIBLE : View.GONE);
        hold.setVisibility(CallRules.canHold(state) || CallRules.canUnhold(state)
                ? View.VISIBLE : View.GONE);

        PcInCallService s = PcInCallService.INSTANCE;
        boolean isMuted = s != null && s.muted();
        boolean isSpeaker = s != null && s.speaker();
        boolean held = state == CallRules.STATE_HOLDING;
        pill(mute, R.drawable.ic_pc_mic, isMuted);
        pill(speaker, R.drawable.ic_pc_volume, isSpeaker);
        pill(keypadBtn, R.drawable.ic_pc_grid, padOpen);
        pill(hold, R.drawable.ic_pc_pause, held);

        if (padOpen && CallRules.canSendTones(state)) {
            pad.setVisibility(View.VISIBLE);
            Keypad.build(this, pad, pal, 52, new Keypad.Press() {
                @Override public void onKey(char digit) { PcInCallService.tone(call(), digit); }
            });
        } else {
            pad.setVisibility(View.GONE);
        }

        drawStatus();
        if (!num.equals(shownFor)) { shownFor = num; paintContext(num); }
    }

    /** An active control takes the accent; an inactive one takes the panel. */
    private void pill(ImageView v, int icon, boolean on) {
        v.setBackground(Skin.pill(this, pal, on ? pal.accent : Skin.alpha(pal.text, 0.12), true));
        v.setImageDrawable(tint(icon, on ? pal.onAccent() : pal.text));
    }

    private void drawStatus() {
        Call c = call();
        if (c == null) return;
        int state = PcInCallService.stateOf(c);
        long since = PcInCallService.connectedAt(c);
        if (state == CallRules.STATE_ACTIVE && since > 0) {
            long secs = Math.max(0, (System.currentTimeMillis() - since) / 1000);
            status.setText(String.format(java.util.Locale.ROOT, "%d:%02d", secs / 60, secs % 60));
        } else {
            status.setText(CallRules.label(state, PcInCallService.isIncoming(c)));
        }
    }

    /** "You have a meeting with them at 3." Off the main thread — it reads the address book. */
    private void paintContext(final String num) {
        new Thread(new Runnable() {
            @Override public void run() {
                final String line = CalendarPeek.nextWith(InCallActivity.this, num);
                main.post(new Runnable() {
                    @Override public void run() {
                        if (!num.equals(shownFor)) return;
                        context.setVisibility(line.isEmpty() ? View.GONE : View.VISIBLE);
                        if (!line.isEmpty()) context.setText(line);
                    }
                });
            }
        }, "pc-tel-context").start();
    }

    /** So a notification tapped while this is up does not stack a second copy of the call screen. */
    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        draw();
    }

    /** Unused today; kept so a "message instead" action has one obvious place to go. */
    void textInstead(String num) {
        try {
            startActivity(new Intent(Intent.ACTION_SENDTO, Uri.parse("smsto:" + Uri.encode(num)))
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
        } catch (Throwable ignored) { }
    }
}
