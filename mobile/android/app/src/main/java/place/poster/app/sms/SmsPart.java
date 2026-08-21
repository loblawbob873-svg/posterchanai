package place.poster.app.sms;

/**
 * ONE ATTACHMENT OF A PICTURE MESSAGE. Plain data, no Android — because its `key` is part of a
 * message's IDENTITY (see SmsKeys.docId) and identity is computed in two languages.
 *
 * The BYTES are deliberately not here. A thread of picture messages is tens of megabytes, and a
 * list screen that carried them would allocate the lot to draw a row of names; `MmsStore.partBytes`
 * fetches one part when something is actually going to show it.
 */
public final class SmsPart {
    /** The row in `content://mms/part`. LOCAL TO THIS PHONE — never published, for SmsMsg.id's reason. */
    public long id;
    /** `image/jpeg`, `video/3gpp`, … as the sender's phone declared it. */
    public String ct = "";
    /** The filename the sender's phone gave it, when it gave one. */
    public String name = "";
    /** How many bytes, or -1 when the provider would not say — which is never "empty". */
    public long bytes = -1;

    /**
     * THIS PART'S SHARE OF THE MESSAGE'S IDENTITY.
     *
     * A picture message often has NO text, so `docId`'s who/when/direction/body is the same string
     * for two photos sent in one second — and two different photos would be filed at one address,
     * which archives one of them and loses the other. The content type, the name the SENDER's phone
     * chose and the length are all carried in the PDU, so they are the same on every device that
     * receives it rather than local numbering like the row id.
     */
    public String key() { return SmsKeys.partKey(ct, name, bytes); }

    public boolean isText() { return ct != null && ct.startsWith("text/"); }
    /** SMIL is the message's LAYOUT, not a thing anybody sent. Shown, it is a wall of XML. */
    public boolean isSmil() { return "application/smil".equalsIgnoreCase(ct); }
}
