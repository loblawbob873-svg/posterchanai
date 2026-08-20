package place.poster.app.sms;

import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Locale;

/**
 * WHEN ARE TWO TEXTS THE SAME TEXT, AND WHEN ARE TWO NUMBERS THE SAME PERSON.
 *
 * Pure — no Android — because both questions are answered in two places that must agree: on the
 * phone, where a message is a row in the system provider, and on every other device, where the same
 * message is an encrypted Nostr document. If the two disagree about identity the archive fills with
 * duplicates, and a delete on the laptop removes a document the phone will simply publish again.
 *
 * THE PROVIDER IS AUTHORITATIVE ON THE DEVICE. Only the default messaging app may write the SMS
 * provider, and it must — every other app and every backup on the phone reads it. The Nostr copy is
 * an ARCHIVE across devices, never a replacement, and the id below is what ties one to the other.
 *
 * The id is derived from the message itself (who, when, which way, what it says) rather than from the
 * provider's row id, and that is the load-bearing choice: a row id is local to one phone, so a
 * restored backup, a second device or a re-imported thread would each mint fresh ids for messages
 * that already have them.
 */
public final class SmsKeys {

    private SmsKeys() { }

    /**
     * A phone number reduced to what can be compared. Everything that is not a digit goes, except a
     * leading `+`; the rest is kept whole so a short code (`22000`) stays itself.
     *
     * Deliberately NOT PhoneNumberUtils.normalizeNumber: that lives on the device and this class is
     * run by tests and mirrored by nothing. `sameNumber` below is where the fuzzy comparison lives.
     */
    public static String normalize(String raw) {
        if (raw == null) return "";
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < raw.length(); i++) {
            char c = raw.charAt(i);
            if (c >= '0' && c <= '9') b.append(c);
            else if (c == '+' && b.length() == 0) b.append(c);
        }
        return b.toString();
    }

    /**
     * Are these the same person? The last SEVEN digits, which is the rule the platform's own
     * PhoneNumberUtils.compare settles on for a loose match, and it is the right amount of loose:
     * the same contact is written `+1 555 010 4477`, `(555) 010-4477` and `5550104477` by three
     * different apps on one phone, and a thread that splits into three is a thread nobody can read.
     *
     * Shorter than seven digits (a short code, a service number) must match EXACTLY — every
     * five-digit shortcode would otherwise be the same conversation.
     */
    public static boolean sameNumber(String a, String b) {
        String x = digits(normalize(a)), y = digits(normalize(b));
        if (x.isEmpty() || y.isEmpty()) return x.equals(y);
        if (x.length() < 7 || y.length() < 7) return x.equals(y);
        return x.substring(x.length() - 7).equals(y.substring(y.length() - 7));
    }

    private static String digits(String s) {
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if (c >= '0' && c <= '9') b.append(c);
        }
        return b.toString();
    }

    /**
     * The key a conversation is filed under, for a thread with one or many participants. Sorted, so
     * a group message reaching two devices in a different order is still one thread.
     */
    public static String threadKey(List<String> addresses) {
        List<String> norm = new ArrayList<String>();
        if (addresses != null) {
            for (String a : addresses) {
                String n = normalize(a);
                if (!n.isEmpty() && !norm.contains(n)) norm.add(n);
            }
        }
        Collections.sort(norm);
        StringBuilder b = new StringBuilder();
        for (String n : norm) { if (b.length() > 0) b.append(','); b.append(n); }
        return b.toString();
    }

    /**
     * THE ARCHIVE'S ADDRESS FOR ONE MESSAGE: `pcai:sms:<24 hex>`.
     *
     * Kind 30078 with a `d` tag, which is the shape Notes uses — deliberately, because that kind
     * already carries every auto-cleaner exemption this history needs and each of those was a total
     * silent loss when Notes learned it the hard way:
     *   * the relay's NIP-40 expiration sweep skips 30078 (and DROPS the tag at ingest, since a
     *     stored expiration hides an event from every read — intact on disk and invisible is worse
     *     than deleted);
     *   * the paid-retention tier's `kind IN (_PRUNABLE_KINDS)` qualifier spares it;
     *   * the CLIENT cache's newest-N eviction is what still has to be told, by prefix, in
     *     store.js's `_isPinned` — a few minutes of reading the global feed otherwise pushes a
     *     year of texts out of a 3000-event window.
     *
     * SECOND-RESOLUTION TIME, not milliseconds. The provider stores milliseconds, a Nostr event
     * stores seconds, and a message re-read from a restored backup can come back with a rounded
     * timestamp — so the id is built from seconds and the two copies still agree.
     */
    public static String docId(String address, long dateMs, String body, boolean incoming) {
        String canon = normalize(address) + "\n" + (dateMs / 1000L) + "\n"
                + (incoming ? "in" : "out") + "\n" + (body == null ? "" : body);
        return "pcai:sms:" + sha256(canon).substring(0, 24);
    }

    /** The tombstone for a deleted message keeps the SAME address — see the class comment. */
    public static boolean isSmsDoc(String d) { return d != null && d.startsWith("pcai:sms:"); }

    /** A send asked for by another device: `pcai:smsout:<24 hex>` of who, what and when it was asked. */
    public static String outboxId(String address, String body, long askedMs) {
        return "pcai:smsout:" + sha256(normalize(address) + "\n" + askedMs + "\n"
                + (body == null ? "" : body)).substring(0, 24);
    }

    public static String sha256(String s) {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] out = md.digest(s.getBytes("UTF-8"));
            StringBuilder b = new StringBuilder();
            for (byte x : out) b.append(String.format(Locale.ROOT, "%02x", x));
            return b.toString();
        } catch (Exception e) {
            // Cannot happen on any Android: SHA-256 and UTF-8 are both required by the platform. If
            // it somehow did, a length-stable fallback keeps the id well-formed rather than throwing
            // inside a broadcast receiver that is holding somebody's text message.
            StringBuilder b = new StringBuilder(Integer.toHexString(s.hashCode()));
            while (b.length() < 64) b.append('0');
            return b.substring(0, 64);
        }
    }

    /**
     * The single body of a message that arrived in several parts. GSM splits anything over 160
     * characters and the parts arrive as separate PDUs in ONE broadcast; concatenating them is the
     * whole job, and getting it wrong means a long text stored as its first 160 characters.
     */
    public static String joinParts(List<String> bodies) {
        StringBuilder b = new StringBuilder();
        if (bodies != null) for (String s : bodies) if (s != null) b.append(s);
        return b.toString();
    }

    /**
     * How many SMS a body will cost to send, for the character counter. GSM-7 fits 160 in one part
     * and 153 per part once it is split (the rest is the concatenation header); anything containing a
     * character outside the GSM alphabet is sent as UCS-2 at 70, then 67.
     *
     * Approximate ON PURPOSE — the exact GSM-7 table has escape sequences that cost two septets — and
     * approximate in the safe direction: the counter may say two when the carrier sends one, never
     * the reverse.
     */
    public static int segments(String body) {
        if (body == null || body.isEmpty()) return 1;
        boolean unicode = false;
        for (int i = 0; i < body.length(); i++) {
            char c = body.charAt(i);
            if (c > 0x7F && "£¥èéùìòÇØøÅåΔΦΓΛΩΠΨΣΘΞÆæßÉÄÖÑÜ§¿äöñüà€".indexOf(c) < 0) { unicode = true; break; }
        }
        int single = unicode ? 70 : 160, multi = unicode ? 67 : 153;
        int n = body.length();
        if (n <= single) return 1;
        return (n + multi - 1) / multi;
    }
}
