package place.poster.app.sms;

/** The row a share's "to whom?" list shows for a conversation. Pure, so tests/test_android_share_to_texts.py runs it. */
final class SendToPick {
    private SendToPick() { }

    /** The contact's name when the list resolved one; else the number(s) -- a group's "a;b" reads "a, b". */
    static String label(String label, String address) {
        String l = label == null ? "" : label.trim();
        String a = address == null ? "" : address.trim();
        if (!l.isEmpty() && !l.equals(a)) return l;
        return a.isEmpty() ? "?" : a.replaceAll("\\s*[;,]\\s*", ", ");
    }
}
