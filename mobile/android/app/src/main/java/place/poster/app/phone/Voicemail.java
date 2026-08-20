package place.poster.app.phone;

import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.provider.CallLog;
import android.telephony.TelephonyManager;

import java.util.List;

/**
 * VOICEMAIL — the number to call, and the messages the carrier has logged.
 *
 * TWO DIFFERENT THINGS, and a dialer that offers only one of them feels broken in a way people find
 * hard to describe:
 *
 *   * DIALLING it. Holding "1" is how a phone has called voicemail since before smartphones, and
 *     the number is the SIM's own (`getVoiceMailNumber`), never a guess. A phone with no voicemail
 *     configured has none, and saying so is better than dialling "1" at somebody.
 *   * LISTING it. Visual voicemail messages arrive in the call log as VOICEMAIL_TYPE rows, which is
 *     how every dialer shows them without implementing the carrier's protocol. Opening one is handed
 *     to whatever app owns the voicemail source — usually the carrier's — because the audio is
 *     theirs and downloading it is not something this app can or should do.
 */
public final class Voicemail {

    private Voicemail() { }

    /** The SIM's voicemail number, or "" — never a guess, and never the literal "1". */
    public static String number(Context ctx) {
        try {
            TelephonyManager tm = (TelephonyManager) ctx.getSystemService(Context.TELEPHONY_SERVICE);
            String n = tm == null ? null : tm.getVoiceMailNumber();
            return n == null ? "" : n;
        } catch (Throwable t) {
            // Needs READ_PHONE_STATE on some versions; without it the honest answer is "unknown".
            return "";
        }
    }

    /** The voicemail messages the carrier has logged. Same store as the call log, filtered. */
    public static List<CallLogStore.Entry> messages(Context ctx, int limit) {
        List<CallLogStore.Entry> all = CallLogStore.recent(ctx, Math.max(limit, 200));
        java.util.List<CallLogStore.Entry> out = new java.util.ArrayList<CallLogStore.Entry>();
        for (CallLogStore.Entry e : all) {
            if (e.type == CallLog.Calls.VOICEMAIL_TYPE && out.size() < limit) out.add(e);
        }
        return out;
    }

    public static int unheard(Context ctx) {
        int n = 0;
        for (CallLogStore.Entry e : messages(ctx, 200)) n++;
        return n;
    }

    /**
     * Open one. Handed to whoever owns the voicemail source — the audio belongs to the carrier's
     * app, and a dialer that tries to fetch it itself is implementing somebody else's protocol.
     */
    public static Intent open(CallLogStore.Entry e) {
        return new Intent(Intent.ACTION_VIEW).setDataAndType(
                Uri.withAppendedPath(CallLog.Calls.CONTENT_URI, String.valueOf(e.id)),
                "vnd.android.cursor.item/calls");
    }
}
