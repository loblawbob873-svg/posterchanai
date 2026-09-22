package place.poster.app.sms;

import java.util.ArrayList;
import java.util.List;

/**
 * WHAT MAKES A GROUP CONVERSATION LOOK LIKE ONE. Pure text rules, no Android — so they are RUN by
 * tests/test_android_texts_group.py on a JVM rather than asserted as source strings.
 *
 * Reported, in the user's words: "I am in a group chat and had no idea when I opened it — it only
 * showed one number", "you have no idea who is saying what", "you only see the latest replier when
 * you open the message up so it looks like a regular convo."
 *
 * All three are the same root cause. The store reads every participant (SmsStore.fillRecipients
 * resolves the whole recipient_ids list against canonical-addresses) and then throws them away:
 * `t.address = people.get(0)` and a joined `everyone` string that only the LIST row ever used.
 * ThreadActivity was handed a single `address` string, so the open conversation titled itself with
 * one participant, drew every incoming bubble identically whoever sent it, and sent a reply to that
 * one person — a private answer to a group question, with nothing on screen saying so.
 *
 * The rules live here rather than in the two activities because both screens and both halves have
 * to agree about what "a group" is; a second hand-written copy is how one screen says "Alice" and
 * the other says "Alice, Bob & 2 more" about the same thread.
 */
public final class SmsGroup {

    private SmsGroup() { }

    /** How many names a title spells out before it counts the rest. */
    public static final int NAMED = 3;

    public static boolean isGroup(List<String> people) {
        return people != null && people.size() > 1;
    }

    /**
     * The conversation's title: the people in it, not the one who spoke last.
     *
     * A title has one line, so a seven-person thread cannot be a list of seven names — but the
     * count MUST survive the truncation, because "Alice, Bob, Carol" ending in an ellipsis reads as
     * a long name, while "& 4 more" is the fact that this is a group.
     */
    public static String title(List<String> labels) {
        if (labels == null || labels.isEmpty()) return "";
        if (labels.size() == 1) return labels.get(0);
        StringBuilder b = new StringBuilder();
        int named = Math.min(NAMED, labels.size());
        // Exactly one left over is spelled out: "& 1 more" is longer than the name it hides.
        if (labels.size() == NAMED + 1) named = labels.size();
        for (int i = 0; i < named; i++) {
            if (i > 0) b.append(", ");
            b.append(labels.get(i));
        }
        int rest = labels.size() - named;
        if (rest > 0) b.append(" & ").append(rest).append(" more");
        return b.toString();
    }

    /** The line under the title. Always says the word "group" — that is the whole point of it. */
    public static String subtitle(int people) {
        if (people <= 1) return "";
        return "Group · " + people + " people";
    }

    /**
     * Does this message need to say WHO sent it?
     *
     * Only in a group, only for messages that arrived, and only when the sender changed — labelling
     * every bubble in a run of five from the same person is noise that makes the one line that
     * matters (the change of speaker) harder to see, which is the complaint restated.
     */
    public static boolean showsSender(boolean group, SmsMsg prev, SmsMsg cur) {
        if (!group || cur == null || !cur.incoming()) return false;
        if (prev == null || !prev.incoming()) return true;
        return !sameSender(prev.address, cur.address);
    }

    /**
     * Through SmsKeys.sameNumber, the app's ONE answer to "are these the same person" — the same
     * seven-digit match the conversation list folds on. Comparing normalized strings instead looks
     * right and is not: "+15550101111" and "(555) 010-1111" are one person written by two apps on
     * one phone, and read as two they label every other message with a name that never changes.
     */
    public static boolean sameSender(String a, String b) {
        if (a == null || b == null || a.trim().isEmpty() || b.trim().isEmpty()) return false;
        return SmsKeys.sameNumber(a, b);
    }

    /**
     * A stable colour slot per participant, so the same person is the same colour all the way down
     * the thread and across a reopen. Derived from the NORMALIZED number, never from the position
     * in the list — a participant list can come back in a different order, and a colour that moves
     * is worse than no colour at all (it says two people are one).
     */
    public static int colorSlot(String address, int slots) {
        if (slots <= 0) return 0;
        // matchKey, not the raw number: the same person written two ways must be one colour.
        String key = SmsKeys.matchKey(address == null ? "" : address);
        int h = 0;
        for (int i = 0; i < key.length(); i++) h = h * 31 + key.charAt(i);
        return Math.abs(h % slots);
    }

    /**
     * Who a reply goes to. In a group that is EVERYONE, which is the difference between joining a
     * conversation and sending somebody a private message they cannot place.
     *
     * `me` is dropped when the platform lists this phone among the participants (it does on some
     * carriers), because a message addressed to yourself comes back as a second copy of your own
     * reply — but only when there is somebody else left to send to, since being wrong about which
     * number is "me" must never turn a reply into a message addressed to nobody.
     */
    public static List<String> replyTo(List<String> people, String fallback, String me) {
        List<String> out = new ArrayList<String>();
        if (people != null) {
            for (String p : people) {
                if (p == null || p.trim().isEmpty()) continue;
                if (me != null && !me.isEmpty() && sameSender(p, me)) continue;
                if (!contains(out, p)) out.add(p.trim());
            }
        }
        if (out.isEmpty() && fallback != null && !fallback.trim().isEmpty()) out.add(fallback.trim());
        return out;
    }

    private static boolean contains(List<String> have, String candidate) {
        for (String h : have) if (sameSender(h, candidate)) return true;
        return false;
    }
}
