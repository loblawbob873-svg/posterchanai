package place.poster.app.sms;

import java.util.Collection;
import java.util.LinkedHashSet;
import java.util.Set;

/** A socket enqueue is not a relay receipt. Every document must have a positive OK. */
public final class SmsArchiveDelivery {
    private final Set<String> waiting = new LinkedHashSet<>();
    private final boolean nonempty;
    public SmsArchiveDelivery(Collection<String> ids) {
        waiting.addAll(ids);
        nonempty = !waiting.isEmpty();
    }
    public boolean accept(String id, boolean accepted) {
        return accepted && waiting.remove(id);
    }
    public boolean needs(String id) { return waiting.contains(id); }
    public boolean complete() { return nonempty && waiting.isEmpty(); }
}
