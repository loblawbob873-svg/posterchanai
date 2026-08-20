package place.poster.app.phone;

/**
 * WHAT A DIALPAD DOES TO WHAT YOU TYPE. Pure, so the awkward cases are RUN rather than assumed.
 *
 * A dialpad looks like the simplest screen in a phone and carries three rules that are easy to get
 * wrong and invisible when you do:
 *
 *   * `+` is only a `+` in the FIRST position. Held on the zero key anywhere else it is part of a
 *     number nobody can call.
 *   * `,` and `;` are PAUSES, not punctuation — a number like `+15550100,,1234` dials the extension
 *     after the call connects. Stripping them "to clean up the number" quietly breaks every stored
 *     phone-tree shortcut somebody has.
 *   * A `tel:` URI must be ENCODED. `#` is a fragment separator, so `*21#` handed to Uri.parse
 *     unencoded becomes `*21` and the person's call-forwarding code silently does something else.
 */
public final class Dial {

    /** The characters a phone number may contain. Everything else typed is dropped. */
    private static final String DIALABLE = "0123456789*#+,;N";

    private Dial() { }

    /** Append one key press, applying the `+`-only-at-the-front rule. */
    public static String press(String current, char key) {
        String s = current == null ? "" : current;
        if (DIALABLE.indexOf(key) < 0) return s;
        if (key == '+' && !s.isEmpty()) return s;
        return s + key;
    }

    public static String backspace(String current) {
        if (current == null || current.isEmpty()) return "";
        return current.substring(0, current.length() - 1);
    }

    /** Only what a radio can dial. Keeps `+`, the pause characters and the GSM service codes. */
    public static String clean(String raw) {
        if (raw == null) return "";
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < raw.length(); i++) {
            char c = raw.charAt(i);
            if (DIALABLE.indexOf(c) < 0) continue;
            if (c == '+' && b.length() > 0) continue;
            b.append(c);
        }
        return b.toString();
    }

    /**
     * A `tel:` URI, encoded.
     *
     * `Uri.encode` is applied by the CALLER against the real android.net.Uri (this class has no
     * Android in it); what happens here is the part that can be got wrong offline — the number is
     * cleaned first, so a name pasted out of a contact card does not become a URI nobody can dial.
     */
    public static String telPart(String raw) { return clean(raw); }

    /**
     * A GSM SERVICE CODE, not a phone number — `*#06#`, `*21*number#`, `**04*…`.
     *
     * They must go through ACTION_DIAL rather than being placed as a call: the platform's own dialer
     * intercepts them and shows the result (an IMEI, a forwarding confirmation). Placed as a call
     * they either fail or, worse, silently change a network setting with nothing shown.
     */
    public static boolean isServiceCode(String raw) {
        String s = clean(raw);
        return s.length() >= 3 && (s.startsWith("*") || s.startsWith("#")) && s.endsWith("#");
    }

    /**
     * A LOOSE, LOCAL-ONLY prettifier for display. Deliberately does NOT try to be
     * PhoneNumberUtils.formatNumber, which is locale-driven and gets it wrong for anybody whose SIM
     * and address book disagree — a number shown wrong is worse than a number shown plainly.
     * Groups only the obvious North-American and plain-11-digit shapes, and otherwise returns the
     * digits untouched.
     */
    public static String pretty(String raw) {
        String s = clean(raw);
        if (s.indexOf(',') >= 0 || s.indexOf(';') >= 0 || s.indexOf('*') >= 0 || s.indexOf('#') >= 0) return s;
        String digits = s.startsWith("+") ? s.substring(1) : s;
        if (!digits.matches("[0-9]+")) return s;
        if (digits.length() == 10) {
            return digits.substring(0, 3) + " " + digits.substring(3, 6) + " " + digits.substring(6);
        }
        if (digits.length() == 11 && s.startsWith("+")) {
            return "+" + digits.charAt(0) + " " + digits.substring(1, 4) + " "
                 + digits.substring(4, 7) + " " + digits.substring(7);
        }
        return s;
    }

    /** Is there anything worth dialling? A string of pauses is not a number. */
    public static boolean dialable(String raw) {
        String s = clean(raw);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            if ((c >= '0' && c <= '9') || c == '*' || c == '#') return true;
        }
        return false;
    }
}
