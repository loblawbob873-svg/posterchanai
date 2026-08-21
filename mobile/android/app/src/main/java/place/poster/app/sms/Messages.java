package place.poster.app.sms;

import android.content.Context;

import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.List;

/**
 * TEXTS AND PICTURE MESSAGES AS ONE CONVERSATION, which is the only way anybody has ever read them.
 *
 * They live in two providers (`content://sms` and `content://mms`) with two column sets, two message
 * -box vocabularies and two TIME UNITS. What ties them together is `thread_id`: both write into the
 * same `threads` table, so a conversation containing both has one id on the phone and the merge is a
 * sort rather than a match.
 *
 * ONE PLACE, because there are three callers — the WebView plugin, the native thread list and the
 * native thread — and a conversation that interleaves correctly on one screen and not on another is
 * the same bug reported three times.
 *
 * THE TWO READS FAIL INDEPENDENTLY AND ARE REPORTED INDEPENDENTLY. Several OEM builds guard the MMS
 * tables differently from the SMS ones, so "I could not read your picture messages" is a real state
 * on a phone whose texts read perfectly — and folding it into one `refused` would either blame the
 * whole screen for a working half or hide a refusal entirely. Both are asked for by name.
 */
public final class Messages {

    private Messages() { }

    private static final Comparator<SmsMsg> NEWEST_FIRST = new Comparator<SmsMsg>() {
        public int compare(SmsMsg a, SmsMsg b) {
            if (a.date != b.date) return a.date > b.date ? -1 : 1;
            // A STABLE TIE-BREAK. A text and a picture stored in the same millisecond would
            // otherwise swap places between two reads of the same store, and a thread whose order
            // changes when you reopen it reads as messages appearing and disappearing.
            return a.id == b.id ? 0 : (a.id > b.id ? -1 : 1);
        }
    };

    /** Everything on the phone, newest first, both kinds. */
    public static List<SmsMsg> recent(Context ctx, int limit) {
        return merge(SmsStore.recent(ctx, limit), MmsStore.recent(ctx, limit), limit);
    }

    /**
     * Everything after a timestamp, OLDEST FIRST — what the archive publishes.
     *
     * Milliseconds on both sides. `MmsStore.since` divides by a thousand for its own column; a
     * caller must never have to know which table it is asking about.
     */
    public static List<SmsMsg> since(Context ctx, long dateMs, int limit) {
        List<SmsMsg> out = merge(SmsStore.since(ctx, dateMs, limit),
                                 MmsStore.since(ctx, dateMs, limit), limit);
        Collections.reverse(out);
        return out;
    }

    /** One conversation, oldest first — the order a thread is read in. */
    public static List<SmsMsg> thread(Context ctx, long threadId, int limit) {
        return thread(ctx, new long[]{ threadId }, limit);
    }

    /**
     * One conversation across every thread id it is spread over — see SmsStore.fold. Both halves
     * are read with the SAME id set: a person split into two thread ids is split in the picture
     * table too, so passing one id there would drop pictures the same way it dropped texts.
     */
    public static List<SmsMsg> thread(Context ctx, long[] threadIds, int limit) {
        List<SmsMsg> out = merge(SmsStore.thread(ctx, threadIds, limit),
                                 MmsStore.thread(ctx, threadIds, limit), limit);
        Collections.reverse(out);
        return out;
    }

    /** The conversation list, with picture messages counted in the snippet and the unread count. */
    public static List<SmsStore.Thread> threads(Context ctx, int limit, boolean withNames) {
        // THE PROVIDER'S OWN CONVERSATION LIST FIRST -- the same table Fossify Messages reads, and
        // the reason its conversations were right on a phone where ours were not. Folding the list
        // out of the messages is a second opinion about something the platform already maintains,
        // and every disagreement reads as a missing, duplicated or half-empty conversation.
        List<SmsStore.Thread> real = SmsStore.platformThreads(ctx, limit, withNames);
        if (!real.isEmpty()) return real;
        // Empty means the table could not be read -- an OEM that guards it, or a phone with no
        // conversations at all. Folding answers both without claiming there is nothing there.
        return SmsStore.fold(ctx, recent(ctx, Math.max(limit, 200)), withNames);
    }

    /**
     * THE TRUNCATION HAPPENS AFTER THE MERGE, and it has to.
     *
     * Each half is read newest-first with its own limit, so taking the newest N of the two lists
     * together is the newest N of the store. Truncating either half BEFORE the merge would drop
     * recent texts to make room for old pictures — a thread with a gap in it, and nothing anywhere
     * to say a message was left out.
     */
    // Package-private, not private: tests/test_android_mms.py RUNS this against generated
    // lists. It touches no Context, so it loads and runs on a plain JVM.
    static List<SmsMsg> merge(List<SmsMsg> sms, List<SmsMsg> mms, int limit) {
        List<SmsMsg> out = new ArrayList<SmsMsg>(sms.size() + mms.size());
        out.addAll(sms);
        out.addAll(mms);
        Collections.sort(out, NEWEST_FIRST);
        int want = Math.max(1, limit);
        return out.size() > want ? new ArrayList<SmsMsg>(out.subList(0, want)) : out;
    }
}
