package place.poster.app.phone;

/**
 * WHAT A CALL SCREEN IS ALLOWED TO DO, given what the call is doing. No Android, so it can be RUN.
 *
 * Audio routing and call state are where a dialer goes wrong, and the failures are not exceptions —
 * they are a button that does nothing, or a button that does the wrong thing at the wrong moment.
 * "Answer" on a call that is already connected, "hold" on one that is still ringing, and "hang up"
 * on a call the network has already ended are each a tap that reports nothing and changes nothing.
 *
 * So the legality of every control is decided here, from the state alone, and
 * tests/test_android_dialer.py runs the table. `PcInCallService` and `InCallActivity` do nothing but
 * obey it.
 *
 * The state numbers are android.telecom.Call.STATE_*, repeated as constants rather than imported so
 * this file stays free of the platform. They are part of the public API and have not moved since
 * API 23; `tests/test_android_dialer.py` checks them against android.jar rather than trusting that.
 */
public final class CallRules {

    public static final int STATE_NEW = 0;
    public static final int STATE_DIALING = 1;
    public static final int STATE_RINGING = 2;
    public static final int STATE_HOLDING = 3;
    public static final int STATE_ACTIVE = 4;
    public static final int STATE_DISCONNECTED = 7;
    public static final int STATE_SELECT_PHONE_ACCOUNT = 8;
    public static final int STATE_CONNECTING = 9;
    public static final int STATE_DISCONNECTING = 10;
    public static final int STATE_PULLING_CALL = 11;
    public static final int STATE_AUDIO_PROCESSING = 12;
    public static final int STATE_SIMULATED_RINGING = 13;

    private CallRules() { }

    /** Somebody is calling and has not been answered. The only state where "answer" means anything. */
    public static boolean canAnswer(int state) {
        return state == STATE_RINGING || state == STATE_SIMULATED_RINGING;
    }

    /**
     * REJECT AND HANG UP ARE DIFFERENT ACTIONS, and using one for the other is a real bug rather
     * than a nicety: rejecting a ringing call can send it to voicemail and, on a phone whose default
     * messages app offers it, a canned text; disconnecting one just drops it. The platform has two
     * methods for exactly this reason.
     */
    public static boolean canReject(int state) { return canAnswer(state); }

    /** Ending a call that is up, dialing, or on hold. Never one already disconnecting. */
    public static boolean canHangUp(int state) {
        return state == STATE_ACTIVE || state == STATE_DIALING || state == STATE_HOLDING
            || state == STATE_CONNECTING || state == STATE_PULLING_CALL || canAnswer(state);
    }

    /** Hold is only meaningful once there is a connection to hold. */
    public static boolean canHold(int state) { return state == STATE_ACTIVE; }

    public static boolean canUnhold(int state) { return state == STATE_HOLDING; }

    /**
     * The keypad sends DTMF, which only reaches anything once the call is UP. A dialing call
     * swallows every tone — the phone-tree digits somebody typed while it rang are simply lost, with
     * the keypad drawing them the whole time.
     */
    public static boolean canSendTones(int state) { return state == STATE_ACTIVE; }

    /**
     * The mute and speaker controls belong to the audio route, which exists from the moment the call
     * is connecting — a person turning the speaker on while it rings expects it to still be on when
     * they are answered.
     */
    public static boolean canRoute(int state) {
        return state != STATE_DISCONNECTED && state != STATE_NEW;
    }

    /** True once nothing on the call screen can do anything — time to close it. */
    public static boolean isOver(int state) {
        return state == STATE_DISCONNECTED;
    }

    /** True while the call has not yet connected, so the screen shows a status rather than a timer. */
    public static boolean isPending(int state) {
        return state == STATE_NEW || state == STATE_CONNECTING || state == STATE_DIALING
            || state == STATE_SELECT_PHONE_ACCOUNT || state == STATE_RINGING
            || state == STATE_SIMULATED_RINGING;
    }

    /** A short label, in the person's terms rather than the platform's. */
    public static String label(int state, boolean incoming) {
        switch (state) {
            case STATE_NEW:
            case STATE_CONNECTING:            return "Connecting";
            case STATE_SELECT_PHONE_ACCOUNT:  return "Choose a SIM";
            case STATE_DIALING:               return "Calling";
            case STATE_RINGING:
            case STATE_SIMULATED_RINGING:     return incoming ? "Incoming call" : "Ringing";
            case STATE_ACTIVE:                return "";              // the timer says it
            case STATE_HOLDING:               return "On hold";
            case STATE_PULLING_CALL:          return "Moving the call";
            case STATE_AUDIO_PROCESSING:      return "Screening";
            case STATE_DISCONNECTING:         return "Ending";
            case STATE_DISCONNECTED:          return "Call ended";
            default:                          return "";
        }
    }

    /**
     * WHICH CALL THE SCREEN SHOULD BE SHOWING when there is more than one.
     *
     * A ringing call outranks everything — somebody is waiting for an answer. Then an active call,
     * then anything still connecting, then held. A screen that shows the held call while another one
     * rings is a screen whose hang-up button ends the wrong call.
     *
     * `-1` for a state means "no call".
     */
    public static int rank(int state) {
        if (canAnswer(state)) return 0;
        if (state == STATE_ACTIVE) return 1;
        if (state == STATE_DIALING || state == STATE_CONNECTING || state == STATE_NEW
                || state == STATE_PULLING_CALL || state == STATE_SELECT_PHONE_ACCOUNT) return 2;
        if (state == STATE_HOLDING) return 3;
        if (state == STATE_DISCONNECTING) return 4;
        return 5;
    }

    /** Given the states of every live call, the index of the one to show, or -1. */
    public static int primary(int[] states) {
        int best = -1, bestRank = Integer.MAX_VALUE;
        if (states == null) return -1;
        for (int i = 0; i < states.length; i++) {
            if (isOver(states[i])) continue;
            int r = rank(states[i]);
            if (r < bestRank) { bestRank = r; best = i; }
        }
        return best;
    }
}
