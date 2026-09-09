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

    /** Forget it. The client is gone, or has been told to stop speaking for the push. */
    public static void clear() { dmAt = 0L; }
}
