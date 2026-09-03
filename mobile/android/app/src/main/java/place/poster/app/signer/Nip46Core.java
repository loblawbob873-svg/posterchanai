package place.poster.app.signer;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * The NIP-46 decisions, with nothing platform-shaped in them.
 *
 * WHY THIS FILE EXISTS SEPARATELY FROM THE SERVICE. The same split the folder-sync engine uses, for
 * the same reason: everything that can get the ANSWER wrong lives here and is tested, and everything
 * that can only fail loudly — opening a socket, drawing a notification — stays in the adapter. There
 * is no device on the machine this is written on, so an untested rule is a guess that ships.
 *
 * The rules are a PORT, not a redesign. `Nip46Signer` in static/js/client/app.js has been answering
 * real apps for months and its behaviour is the specification: same permission gate, same "which
 * encryption does this peer speak" rule, same defaults. Where this disagreed with the JS during the
 * port, the JS won. A native signer that answers differently from the one it replaces is a new set
 * of bugs wearing the old one's name.
 */
public final class Nip46Core {

    private Nip46Core() { }

    /** A paired app: who it is, where it is answered, and what it was granted. */
    public static final class Session {
        public final String pk;        // the CLIENT's pubkey, hex — the map key
        public final String relay;     // the one socket this app is answered on
        public final String name;      // for the UI only
        public final String perms;     // the raw comma-separated `perms` from the QR, "" = everything
        public String enc;             // "nip04" | "nip44" — learned from what the peer SENDS
        public long last;

        public Session(String pk, String relay, String name, String perms, String enc, long last) {
            this.pk = pk == null ? "" : pk.toLowerCase();
            this.relay = relay == null ? "" : relay;
            this.name = name == null ? "" : name;
            this.perms = perms == null ? "" : perms;
            this.enc = enc == null ? "" : enc;
            this.last = last;
        }
    }

    /**
     * Is this app allowed to ask for this?
     *
     * A VERBATIM PORT of the JS `_allowed`, including the part that looks too permissive: an app that
     * declared NO `perms` in its QR is granted everything. That is what the JS has always done and
     * what the ecosystem expects — a client that lists nothing is not asking for nothing, it simply
     * did not fill the field in, and refusing those would silently break every such app.
     *
     * `kind` is -1 when the request is not a sign_event or its template could not be read. It must
     * never be defaulted to 0: kind 0 is a profile metadata event, so a client granted
     * `sign_event:0` would be handed every unreadable template as if it had asked for it.
     */
    public static boolean allowed(String permsCsv, String method, int kind) {
        if (method == null) return false;
        String perms = permsCsv == null ? "" : permsCsv.trim();
        if (perms.isEmpty()) return true;                  // declared nothing → no restriction
        // Always answerable: the handshake, a liveness check, and the pubkey — which is public by
        // definition and which every client asks for the moment it connects.
        if (method.equals("connect") || method.equals("ping") || method.equals("get_public_key")) {
            return true;
        }
        for (String g : perms.split(",")) {
            String t = g.trim();
            if (t.isEmpty()) continue;
            if (t.equals(method)) return true;
            if (kind >= 0 && method.equals("sign_event") && t.equals("sign_event:" + kind)) return true;
            // NIP-78 added same-owner NIP-42 AUTH after existing pairings were issued. Permission
            // to sign private app data includes its ephemeral access proof; otherwise every old
            // full pairing loses vault/SMS sync until manually re-paired.
            if (kind == 22242 && method.equals("sign_event") && t.equals("sign_event:30078")) return true;
        }
        return false;
    }

    /**
     * Which decryption to TRY FIRST.
     *
     * `?iv=` is NIP-04's own marker, so the choice is made from evidence in the payload rather than
     * discovered by letting a decrypt fail. Both are still attempted by the caller — this only sets
     * the order — because a peer that marks its payload wrongly should still be understood.
     */
    public static boolean nip04First(String ct) {
        return ct != null && ct.contains("?iv=");
    }

    /**
     * Which encryption to ANSWER in, defaulting to NIP-44.
     *
     * The bug this rule exists for: always answering in NIP-04 is how "the app appears in my signer's
     * list but the site never logs in" happens. We mint the session, so our side looks paired, and
     * then send an acknowledgement the other end cannot read. NIP-46 moved to NIP-44 and current
     * clients — jumble.social, Coracle — may implement only that, so a NIP-04 reply is silence with
     * extra steps. Every later reply uses whatever that peer's request actually ARRIVED in, which is
     * the only evidence that cannot be wrong.
     */
    public static boolean replyWithNip04(String sessionEnc) {
        return "nip04".equals(sessionEnc);
    }

    /**
     * Fold a freshly-published session list into what the service is already running.
     *
     * WHY A MERGE AND NOT A REPLACE. `enc` and `last` are learned by the SERVICE, from traffic the
     * web layer never sees — it is the half that is awake. The web layer publishes the list whenever
     * it changes, and a straight replace would throw that away every time, sending the next reply in
     * the default scheme to a peer already known to speak the other one. That is a silent failure:
     * a well-formed event the far end cannot read.
     *
     * The incoming list is still AUTHORITATIVE about MEMBERSHIP — a revoked app must actually stop
     * being answered, so anything absent from it is dropped. Only the learned fields are carried.
     */
    public static Map<String, Session> merge(Map<String, Session> running, List<Session> incoming) {
        Map<String, Session> out = new LinkedHashMap<>();
        if (incoming == null) return out;
        for (Session in : incoming) {
            if (in == null || in.pk.length() != 64 || in.relay.isEmpty()) continue;
            Session had = running == null ? null : running.get(in.pk);
            if (had != null) {
                if (in.enc.isEmpty()) in.enc = had.enc;
                if (in.last < had.last) in.last = had.last;
            }
            out.put(in.pk, in);
        }
        return out;
    }

    /** Every distinct relay the running sessions need a socket to. */
    public static List<String> relays(Map<String, Session> sessions) {
        List<String> out = new ArrayList<>();
        if (sessions == null) return out;
        for (Session s : sessions.values()) {
            if (s == null || s.relay.isEmpty()) continue;
            if (!out.contains(s.relay)) out.add(s.relay);
        }
        return out;
    }

    /**
     * How long to wait before dialling a relay again, in milliseconds.
     *
     * Capped, and the cap is the point. An uncapped backoff on a phone that was in a tunnel means the
     * signer is still waiting twenty minutes after the signal came back, which the user experiences
     * as exactly the bug this whole service exists to fix. A floor matters too: a relay that refuses
     * instantly would otherwise be redialled in a hot loop and cook the battery.
     */
    public static long backoffMs(int failures) {
        if (failures <= 0) return 2000L;
        long ms = 2000L << Math.min(failures, 5);          // 2s, 4s … 64s
        return Math.min(ms, 60000L);
    }

    /**
     * The `since` a subscription asks for, in seconds.
     *
     * NOT "now". The two ends of a QR pairing are two machines with two clocks by definition, the
     * relay applies `since` server-side, and the requesting app stamps its request with ITS clock —
     * so a desktop a minute behind this phone had every request dropped before it arrived. The phone
     * said "logged in", the desktop sat on "waiting for the signer" until it timed out, and nothing
     * anywhere raised an error, because from this side nothing ever came. The web signer learned this
     * the same way; the constant is deliberately the same number.
     */
    public static final long SINCE_SKEW = 900L;

    public static long since(long nowSec) {
        return Math.max(0L, nowSec - SINCE_SKEW);
    }
}
