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

    /** A two-second pause. What a long press on `*` types, and what `p` in a stored number means. */
    public static final char PAUSE = ',';
    /** Stop and wait to be told to send the rest. Long press on `#`, and `w` in a stored number. */
    public static final char WAIT = ';';

    private Dial() { }

    /** Append one key press, applying the `+`-only-at-the-front rule. */
    public static String press(String current, char key) {
        String s = current == null ? "" : current;
        char k = key == 'p' || key == 'P' ? PAUSE : key == 'w' || key == 'W' ? WAIT : key;
        if (DIALABLE.indexOf(k) < 0) return s;
        if (k == '+' && !s.isEmpty()) return s;
        return s + k;
    }

    /**
     * WHAT A LONG PRESS ON A KEY TYPES, or 0 for the nine keys that hold nothing.
     *
     * This is the rule that was MISSING, and its absence is the whole bug. `Dial` has accepted `,`
     * and `;` since it was written and `clean` has always kept them — but the pad had one long
     * press on it, `+` on the zero key, so there was no way on the entire screen to type a pause.
     * A number with one in it could only ever arrive from a contact card. Support for the
     * characters, and no way to enter them, is indistinguishable from no support at all.
     *
     * `pauses` is off for the IN-CALL pad, which sends DTMF down a live call: a pause is a dialling
     * instruction and means nothing once the call is up, so a key offering one there does nothing.
     */
    public static char held(char key, boolean pauses) {
        if (key == '0') return '+';
        if (!pauses) return 0;
        if (key == '*') return PAUSE;
        if (key == '#') return WAIT;
        return 0;
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
            if (isPauseLetter(c)) {
                // `p` AND `w` ARE PAUSES TOO, and they arrive from places a keypad never touches:
                // a vCard imported from another phone, a carrier's own "1-800-555-0100p1234", a
                // contact typed on a handset whose dialer spells them that way. Dropped as letters,
                // the extension after them ran straight into the number and dialled a stranger.
                //
                // A LETTER IS ONLY A PAUSE BETWEEN TWO DIALABLE CHARACTERS. "555 Powell St" is an
                // address, not a phone tree: read as a pause its `P` would turn a street into
                // `555,`. So both neighbours are checked — what has been kept so far, and what
                // comes next — and anything else is the ordinary letter it looks like.
                if (b.length() > 0 && isDialableNext(raw, i + 1)) b.append(c == 'p' || c == 'P' ? PAUSE : WAIT);
                continue;
            }
            if (DIALABLE.indexOf(c) < 0) continue;
            if (c == '+' && b.length() > 0) continue;
            b.append(c);
        }
        return b.toString();
    }

    private static boolean isPauseLetter(char c) {
        return c == 'p' || c == 'P' || c == 'w' || c == 'W';
    }

    private static boolean isDialableNext(String raw, int from) {
        if (from >= raw.length()) return false;
        char c = raw.charAt(from);
        return DIALABLE.indexOf(c) >= 0 || isPauseLetter(c);
    }

    /**
     * A `tel:` URI, encoded.
     *
     * `telPart` is the number a radio would dial; `telUri` is the whole URI, percent-encoded HERE
     * rather than by `android.net.Uri.encode` at the call site, so the one rule that silently eats
     * a phone-tree number can be RUN by a test on a machine with no Android on it.
     *
     * `#` IS A FRAGMENT SEPARATOR. `Uri.parse("tel:*21#")` keeps `*21` and throws the rest away —
     * so a call-forwarding code, or `+18005550100,,123#`, arrives at the platform as a different
     * number with nothing logged. It must travel as `%23`. `+`, `,` and `;` are encoded for the
     * same reason: they are reserved, and a URI parser is entitled to read them structurally.
     */
    public static String telPart(String raw) { return clean(raw); }

    /** Everything a URI parser may leave alone. Deliberately small: unreserved, plus `*`. */
    private static final String URI_SAFE =
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.~*";

    /** The `tel:` URI for what somebody typed — cleaned, then percent-encoded. */
    public static String telUri(String raw) { return "tel:" + encode(clean(raw)); }

    /** Percent-encode an already-cleaned number. `,` `;` `#` `+` all become escapes. */
    public static String encode(String cleaned) {
        if (cleaned == null) return "";
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < cleaned.length(); i++) {
            char c = cleaned.charAt(i);
            if (URI_SAFE.indexOf(c) >= 0) { b.append(c); continue; }
            b.append('%');
            b.append(Character.toUpperCase(Character.forDigit((c >> 4) & 0xF, 16)));
            b.append(Character.toUpperCase(Character.forDigit(c & 0xF, 16)));
        }
        return b.toString();
    }

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
