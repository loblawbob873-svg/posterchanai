package place.poster.app.sms;

import java.util.*;

/** Conservative view of one complete, strictly identified SMS conversation. No provider writes. */
final class SmsReactionThread {
    final List<SmsMsg> raw;
    final List<SmsMsg> visible = new ArrayList<>();
    final SmsReactions.Projection projection;
    final boolean complete;
    static String id(SmsMsg m) { return (m.mms ? "mms:" : "sms:") + m.id; }
    private static boolean singleNumber(String address) {
        if (address == null || !address.trim().matches("\\+?[0-9(][0-9 ().-]*")) return false;
        String digits = SmsKeys.normalize(address).replace("+", "");
        return !digits.isEmpty() && digits.length() <= 15;
    }
    /** Whether a reaction may be SENT here. True when the history is complete -- or when it was cut off ONLY by
     *  the read window ({@code windowed}) and every other safety rule holds: a reaction is always newer than its
     *  target, so for a target inside the newest-N window every reaction to it is loaded too, and the duplicate-text
     *  check runs over everything loaded. Showing reactions as chips still needs {@link #complete}.
     *  "no way to react on text message, long press says copy and delete" (2026-10-15): any conversation of 501+
     *  messages had React switched off for every message. */
    final boolean reactable;
    SmsReactionThread(List<SmsMsg> rows, boolean historyComplete, long thread, String address) {
        this(rows, historyComplete, false, thread, address);
    }
    SmsReactionThread(List<SmsMsg> rows, boolean historyComplete, boolean windowed, long thread, String address) {
        raw = new ArrayList<>(rows);
        String peer = SmsKeys.normalize(address);
        boolean safe = historyComplete && thread > 0 && singleNumber(address);
        boolean sendSafe = (historyComplete || windowed) && thread > 0 && singleNumber(address);
        List<SmsReactions.Message> messages = new ArrayList<>();
        for (SmsMsg m : rows) {
            // Still one other person, by number and by the MMS participant count. A picture message no
            // longer switches reactions off for the whole conversation: other phones react to pictures
            // ("Liked a photo"), and a thread with one photo in it used to show none of its reactions.
            if (m.threadId != thread || !singleNumber(m.address) || m.people != 1
                    || !peer.equals(SmsKeys.normalize(m.address))) { safe = false; sendSafe = false; }
            messages.add(new SmsReactions.Message(id(m), Long.toString(m.threadId),
                    m.incoming() ? peer : "self", m.body == null ? "" : m.body, m.date, m.incoming(),
                    m.type == 1 || m.type == 2, false, !m.parts.isEmpty()));
        }
        complete = safe;
        reactable = sendSafe;
        projection = SmsReactions.project(messages, safe);
        for (SmsMsg m : rows) if (!projection.consumedIds.contains(id(m))) visible.add(m);
    }
    SmsMsg find(String key) {
        for (SmsMsg m : raw) if (id(m).equals(key)) return m;
        return null;
    }
    SmsReactions.Chip own(SmsMsg m) {
        List<SmsReactions.Chip> chips = projection.chipsByTarget.get(id(m));
        if (chips != null) for (SmsReactions.Chip c : chips) if ("self".equals(c.actor)) return c;
        return null;
    }
    boolean canReact(SmsMsg shown) {
        if (!reactable || shown == null) return false;
        SmsMsg m = find(id(shown));
        if (m == null || !m.docId().equals(shown.docId()) || !m.incoming() || m.mms
                || !m.parts.isEmpty() || m.body.isEmpty() || SmsReactions.parse(m.body) != null) return false;
        int matches = 0;
        for (SmsMsg row : raw) {
            if (row.incoming() && row.body.equals(m.body)) matches++;
            SmsReactions.Parsed p = SmsReactions.parse(row.body);
            if (!row.incoming() && row.pending() && p != null && p.text.equals(m.body)) return false;
        }
        return matches == 1;
    }
}
