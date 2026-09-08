package place.poster.app.signer;

import java.util.ArrayDeque;
import java.util.Iterator;
import java.util.function.BiPredicate;
import java.util.function.Predicate;

/** Owner-thread-only, short-lived delivery retry of already encrypted/signed replies. */
final class SignerReplyQueue<S> {
    private static final int LIMIT = 64;
    private static final long TTL_MS = 120_000L;
    private static final class Reply<S> {
        final S session; final String wire; final long expires;
        Reply(S session, String wire, long expires) {
            this.session=session; this.wire=wire; this.expires=expires;
        }
    }
    private final ArrayDeque<Reply<S>> pending = new ArrayDeque<>();
    boolean add(S session, String wire, long now) {
        expire(now);
        if (pending.size() >= LIMIT) return false;
        pending.add(new Reply<>(session,wire,now+TTL_MS));
        return true;
    }
    private void expire(long now) { pending.removeIf(reply -> reply.expires <= now); }
    int flush(long now, Predicate<S> current, BiPredicate<S,String> send) {
        expire(now);
        int sent=0;
        for (Iterator<Reply<S>> it=pending.iterator();it.hasNext();) {
            Reply<S> reply=it.next();
            if (!current.test(reply.session)) { it.remove(); continue; }
            if (send.test(reply.session,reply.wire)) { it.remove(); sent++; }
        }
        return sent;
    }
    int size() { return pending.size(); }
    void clear() { pending.clear(); }
}
