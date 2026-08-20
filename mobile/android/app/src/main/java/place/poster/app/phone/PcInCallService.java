package place.poster.app.phone;

import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.telecom.Call;
import android.telecom.CallAudioState;
import android.telecom.InCallService;
import android.util.Log;

import java.util.ArrayList;
import java.util.List;

/**
 * THE PHONE'S CALL SCREEN — the app's end of the telecom stack when it is the default dialer.
 *
 * NOT `place.poster.app.call.CallService`, which is a different feature with a confusingly similar
 * name: that one is a foreground service for a Nostr WebRTC call — a call over the INTERNET to
 * another Nostr user. This one is the cellular network. They must never share an intent action, a
 * notification channel or a notification id, because both live in the same process and a hang-up
 * meant for one would tear down the other. Everything here is prefixed `TEL_` and `pcai_cell_` for
 * exactly that reason.
 *
 * WHAT THIS CLASS OWNS: the live call list, and nothing else. Which controls are legal is
 * CallRules', which is pure and run by tests; what the screen looks like is InCallActivity's. The
 * platform hands calls here and takes them away again, and every decision in between is somebody
 * else's.
 *
 * THE INSTANCE IS STATIC because the UI cannot get at the service any other way: Android binds this,
 * not us, and an Activity has no handle on it. The same pattern MusicService and CallService already
 * use, and the same rule applies — read it, do not assume it, because the platform may have unbound
 * us between the draw and the tap.
 */
public class PcInCallService extends InCallService {

    private static final String TAG = "PosterChan";

    public static volatile PcInCallService INSTANCE;

    /** Told when anything about the calls changed, so the screen redraws without polling. */
    public interface Watcher { void onCallsChanged(); }
    private static volatile Watcher watcher;
    public static void setWatcher(Watcher w) { watcher = w; }

    private final List<Call> calls = new ArrayList<Call>();

    private final Call.Callback cb = new Call.Callback() {
        @Override public void onStateChanged(Call call, int state) { changed(); }
        @Override public void onDetailsChanged(Call call, Call.Details details) { changed(); }
        @Override public void onCallDestroyed(Call call) { changed(); }
    };

    @Override
    public void onCallAdded(Call call) {
        super.onCallAdded(call);
        if (call == null) return;
        synchronized (calls) { if (!calls.contains(call)) calls.add(call); }
        try { call.registerCallback(cb); } catch (Throwable ignored) { }
        INSTANCE = this;
        // THE SCREEN IS RAISED FROM HERE, not from the activity's own logic, because this is the
        // only place that knows a call exists. An incoming call also gets a full-screen notification
        // (InCallNotifier): on a locked or dozing device a background activity start is refused
        // SILENTLY, and a phone that rings with nothing on screen is a phone that missed the call.
        try { InCallNotifier.show(this, call); } catch (Throwable t) { Log.w(TAG, "tel: no ring", t); }
        try {
            startActivity(new Intent(this, InCallActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP));
        } catch (Throwable ignored) { }
        changed();
    }

    @Override
    public void onCallRemoved(Call call) {
        super.onCallRemoved(call);
        if (call == null) return;
        try { call.unregisterCallback(cb); } catch (Throwable ignored) { }
        synchronized (calls) { calls.remove(call); }
        boolean none;
        synchronized (calls) { none = calls.isEmpty(); }
        if (none) {
            try { InCallNotifier.clear(this); } catch (Throwable ignored) { }
            // The audio route is per-call and the platform resets it, but the SPEAKER is the one
            // people notice: leaving it on means the next call, and the next notification, come out
            // of the loudspeaker with no way to see why.
            try { setMuted(false); } catch (Throwable ignored) { }
        }
        changed();
    }

    @Override
    public void onCallAudioStateChanged(CallAudioState state) {
        super.onCallAudioStateChanged(state);
        changed();
    }

    @Override
    public void onDestroy() {
        synchronized (calls) {
            for (Call c : calls) { try { c.unregisterCallback(cb); } catch (Throwable ignored) { } }
            calls.clear();
        }
        if (INSTANCE == this) INSTANCE = null;
        super.onDestroy();
    }

    private void changed() {
        Watcher w = watcher;
        if (w != null) { try { w.onCallsChanged(); } catch (Throwable ignored) { } }
        try { InCallNotifier.refresh(this); } catch (Throwable ignored) { }
    }

    // ------------------------------------------------------------------ what the screen asks for

    public List<Call> liveCalls() {
        List<Call> out = new ArrayList<Call>();
        synchronized (calls) {
            for (Call c : calls) if (c != null && !CallRules.isOver(stateOf(c))) out.add(c);
        }
        return out;
    }

    /** The one call the screen should show. See CallRules.primary for why the order matters. */
    public Call primary() {
        List<Call> live = liveCalls();
        if (live.isEmpty()) return null;
        int[] states = new int[live.size()];
        for (int i = 0; i < live.size(); i++) states[i] = stateOf(live.get(i));
        int i = CallRules.primary(states);
        return i < 0 ? null : live.get(i);
    }

    /**
     * A call's state, read defensively.
     *
     * `getState()` is deprecated from API 31 in favour of `getDetails().getState()`, and the
     * deprecated one THROWS on a call the platform has already destroyed — which is precisely the
     * moment a redraw is most likely to be reading it.
     */
    public static int stateOf(Call c) {
        if (c == null) return CallRules.STATE_DISCONNECTED;
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                Call.Details d = c.getDetails();
                if (d != null) return d.getState();
            }
            return c.getState();
        } catch (Throwable t) {
            return CallRules.STATE_DISCONNECTED;
        }
    }

    /** The other party's number, or "" — a withheld number really is absent, not an error. */
    public static String numberOf(Call c) {
        try {
            Call.Details d = c == null ? null : c.getDetails();
            android.net.Uri h = d == null ? null : d.getHandle();
            if (h == null) return "";
            String s = h.getSchemeSpecificPart();
            return s == null ? "" : s;
        } catch (Throwable t) { return ""; }
    }

    public static boolean isIncoming(Call c) {
        try {
            Call.Details d = c == null ? null : c.getDetails();
            return d != null && d.getCallDirection() == Call.Details.DIRECTION_INCOMING;
        } catch (Throwable t) {
            // getCallDirection is API 29. Below it, ringing is the only way a call can arrive.
            return CallRules.canAnswer(stateOf(c));
        }
    }

    /** When the call connected, for the timer. 0 while it has not. */
    public static long connectedAt(Call c) {
        try {
            Call.Details d = c == null ? null : c.getDetails();
            if (d == null) return 0;
            long t = d.getConnectTimeMillis();
            return t > 0 ? t : 0;
        } catch (Throwable t) { return 0; }
    }

    // ------------------------------------------------------------------ actions

    /**
     * Every one of these checks CallRules first, and that is the point of having it. The platform
     * answers a call that cannot be answered by doing nothing at all — no exception, no callback —
     * so a screen that offers the button anyway has a button that silently does nothing.
     */
    public static void answer(Call c) {
        if (!CallRules.canAnswer(stateOf(c))) return;
        try { c.answer(android.telecom.VideoProfile.STATE_AUDIO_ONLY); } catch (Throwable ignored) { }
    }

    /** Reject, not disconnect: it can send the caller to voicemail, which hanging up does not. */
    public static void reject(Call c) {
        if (!CallRules.canReject(stateOf(c))) return;
        try { c.reject(false, null); } catch (Throwable ignored) { }
    }

    public static void hangUp(Call c) {
        if (!CallRules.canHangUp(stateOf(c))) return;
        try { c.disconnect(); } catch (Throwable ignored) { }
    }

    public static void hold(Call c, boolean on) {
        int s = stateOf(c);
        try {
            if (on && CallRules.canHold(s)) c.hold();
            else if (!on && CallRules.canUnhold(s)) c.unhold();
        } catch (Throwable ignored) { }
    }

    public static void tone(Call c, char digit) {
        if (!CallRules.canSendTones(stateOf(c))) return;
        try { c.playDtmfTone(digit); c.stopDtmfTone(); } catch (Throwable ignored) { }
    }

    public boolean muted() {
        try {
            CallAudioState s = getCallAudioState();
            return s != null && s.isMuted();
        } catch (Throwable t) { return false; }
    }

    public boolean speaker() {
        try {
            CallAudioState s = getCallAudioState();
            return s != null && s.getRoute() == CallAudioState.ROUTE_SPEAKER;
        } catch (Throwable t) { return false; }
    }

    public boolean bluetoothAvailable() {
        try {
            CallAudioState s = getCallAudioState();
            return s != null && (s.getSupportedRouteMask() & CallAudioState.ROUTE_BLUETOOTH) != 0;
        } catch (Throwable t) { return false; }
    }

    public void setSpeaker(boolean on) {
        try {
            setAudioRoute(on ? CallAudioState.ROUTE_SPEAKER
                             : CallAudioState.ROUTE_WIRED_OR_EARPIECE);
        } catch (Throwable ignored) { }
    }

    public void toggleMute() {
        try { setMuted(!muted()); } catch (Throwable ignored) { }
    }

    /** For the settings screen and the launcher tile: is there anything going on right now? */
    public static boolean callInProgress() {
        PcInCallService s = INSTANCE;
        return s != null && !s.liveCalls().isEmpty();
    }
}
