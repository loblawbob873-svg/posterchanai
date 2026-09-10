package place.poster.app.push;

/**
 * WHAT THE RUNNING CLIENT HAS ALREADY SAID, so the blind push does not say it again.
 *
 *     "i do not want 2 notifications on my phone for every DM!
 *      'someone sent you a message' then 'bla bla bla sent you a message'"
 *
 * Those two lines are the same message at two stages of knowledge. The server CANNOT decrypt a
 * NIP-17 gift wrap, so its push can only say "Someone"; the client can, so once it has opened the
 * wrap it says who. Neither is wrong and neither can simply be deleted: the push is the only copy
 * that reaches a phone with the app closed, and the client's is the only one that names the sender.
 *
 * So they share an identity instead. Both now post under the tag `pc-dm`, which makes Android
 * REPLACE rather than stack — that alone fixes the ordering where the push lands first and the
 * client fills in the name afterwards.
 *
 * THE OTHER ORDERING NEEDS THIS CLASS. When the client got there first, a shared tag would let the
 * late generic push overwrite "Alice sent you a DM" with "Someone sent you a message" — one card,
 * but the worse one — and if the person had already read and dismissed it, re-post a notification
 * for a message they have finished with. So a live client's own notification is RECORDED here, and
 * `PushEventService.deliver` drops a generic DM push inside that window.
 *
 * IT FAILS OPEN, and that is the whole safety argument. Nothing is ever recorded on a phone whose
 * WebView is not running — which is precisely the phone the push exists for — so "no record" means
 * SHOW. The failure this repo keeps re-learning is a duplicate guard that ends up suppressing the
 * only copy; the window is short and only a client that has demonstrably just drawn its own
 * notification can open one.
 *
 * STATIC, like MusicService.INSTANCE and SmsPlugin.live, because the two halves are a plugin call
 * on the WebView's thread and a delivery on the socket service's, with no instance in common.
 *
 * Pure Java, no Android imports, so tests/test_android_one_dm_notification.py RUNS it.
 */
public final class ClientNotified {

    /**
     * How long a client's own DM notification speaks for the push that follows it.
     *
     * Deliberately shorter than a minute: the cost of being wrong is a DM with no notification at
     * all, and the case that would cause it — the WebView dying in the seconds after it notified —
     * is real. The server collapses a burst per recipient on a 10s cooldown, so a genuinely new
     * conversation is never far behind.
     */
    public static final long WINDOW_MS = 45_000L;

    private static volatile long dmAt = 0L;

    private ClientNotified() { }

    /** The client just drew its own (decrypted, named) DM notification. */
    public static void dm(long now) { dmAt = now; }

    /** Has the client spoken for a DM recently enough that a generic push would be a duplicate? */
    public static boolean recentlyDm(long now) {
        long when = dmAt;
        if (when <= 0L) return false;
        // A clock that has gone backwards must not latch this open — that would silence DMs for as
        // long as the condition lasted, which is the failure mode worse than the duplicate.
        if (now < when) return false;
        return now - when < WINDOW_MS;
    }

    /* WRAPS THIS DEVICE PUBLISHED, so a push for a message you SENT is dropped exactly.
     *
     * "evey time I send a DM i get a push notification." NIP-17 publishes TWO gift wraps for one
     * message — one the peer can open and a SELF-COPY the sender can, so their other devices see
     * what they sent — and both are p-tagged to their own reader. A gift wrap's author is an
     * ephemeral throwaway key, so the server's "don't notify the author" guard cannot tell that the
     * self-copy's recipient IS its sender.
     *
     * The device that published it can. It records the wrap id and drops a push carrying that id.
     * EXACT, not a time window: a window after sending would have silenced a real DM that arrived
     * seconds later, which is the failure this file already argues is worse than the duplicate.
     *
     * Bounded and time-limited: a send is followed by its push within seconds, so an id that has
     * gone unclaimed for a few minutes is never going to be, and the map must not grow for the life
     * of the process. Failing open is preserved — an id we never recorded, or one we have since
     * forgotten, means SHOW.
     */
    public static final long SENT_WINDOW_MS = 300_000L;
    private static final int SENT_MAX = 256;
    private static final java.util.LinkedHashMap<String, Long> sent =
            new java.util.LinkedHashMap<String, Long>(32, 0.75f, false) {
                @Override protected boolean removeEldestEntry(java.util.Map.Entry<String, Long> e) {
                    return size() > SENT_MAX;
                }
            };

    /** This device published `wrapId` itself — a push carrying it is our own message coming back. */
    public static void sent(String wrapId, long now) {
        if (wrapId == null || wrapId.isEmpty()) return;
        synchronized (sent) { sent.put(wrapId, now); }
    }

    /** Did this device publish the wrap this push is about? Consumes it: a push arrives once. */
    public static boolean weSent(String wrapId, long now) {
        if (wrapId == null || wrapId.isEmpty()) return false;
        synchronized (sent) {
            Long when = sent.remove(wrapId);
            if (when == null) return false;
            // A clock that has gone backwards must not make an old id look fresh, and an id older
            // than the window is stale bookkeeping rather than evidence about this push.
            if (now < when || now - when > SENT_WINDOW_MS) return false;
            return true;
        }
    }

    /** Forget it. The client is gone, or has been told to stop speaking for the push. */
    public static void clear() {
        dmAt = 0L;
        synchronized (sent) { sent.clear(); }
    }
}
