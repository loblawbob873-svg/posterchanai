package place.poster.app.sms;

/** One row of the system SMS provider, as this app reads it. Plain data, no Android. */
public final class SmsMsg {
    public long id;
    public long threadId;
    public String address = "";
    public String body = "";
    /** Milliseconds, as the provider stores it. The archive rounds to seconds — see SmsKeys.docId. */
    public long date;
    /** Telephony.Sms.MESSAGE_TYPE_*: 1 inbox, 2 sent, 3 draft, 4 outbox, 5 failed, 6 queued. */
    public int type;
    public boolean read;

    public boolean incoming() { return type == 1; }
    public boolean failed() { return type == 5; }
    public boolean pending() { return type == 4 || type == 6; }

    /** The archive address for this message. Stable across devices and across a restored backup. */
    public String docId() { return SmsKeys.docId(address, date, body, incoming()); }
}
