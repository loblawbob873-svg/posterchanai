package place.poster.app.sms;

import java.util.ArrayList;
import java.util.List;

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
    /** Carrier reason retained for a failed MMS row; empty for SMS and successful MMS. */
    public String error = "";

    /**
     * A PICTURE MESSAGE, READ FROM A DIFFERENT PROVIDER TABLE.
     *
     * `content://mms` and `content://sms` are two tables with two column sets and two time units,
     * and only the row id and the THREAD id are comparable between them. Carried here rather than
     * inferred from `parts` being non-empty, because a picture message whose attachments could not
     * be read is still a picture message and must not be redrawn as a text.
     */
    public boolean mms;
    /**
     * HOW MANY PEOPLE ARE IN THIS CONVERSATION, which is the only thing that separates a group
     * picture message from a one-to-one one: both carry a single `address` (see MmsStore's
     * fillAddresses), so without this a group thread would fold into one member's private thread
     * and put messages other people can see into a conversation that looks private. Plain SMS is
     * always 1. Unknown counts as 1, since an MMS whose address table could not be read is far more
     * often a normal message than a group.
     */
    public int people = 1;
    /**
     * A picture message the carrier has ANNOUNCED but the phone has not fetched. It has no text and
     * no parts and never will until it is retrieved, so a bubble must say that rather than draw
     * nothing. See MmsStore's PDU_NOTIFICATION_IND.
     */
    public boolean undownloaded;

    /**
     * What was attached. Metadata only — the bytes are fetched one at a time, when something is
     * about to show them (MmsStore.partBytes). A thread of picture messages is tens of megabytes.
     */
    public List<SmsPart> parts = new ArrayList<SmsPart>();

    public boolean incoming() { return type == 1; }
    public boolean failed() { return type == 5; }
    public boolean pending() { return type == 4 || type == 6; }

    /** The attachments' share of this message's identity — empty for an ordinary text. */
    public String partsKey() {
        List<String> keys = new ArrayList<String>();
        for (SmsPart p : parts) keys.add(p.key());
        return SmsKeys.partsKey(keys);
    }

    /** The archive address for this message. Stable across devices and across a restored backup. */
    public String docId() {
        return SmsKeys.docId(address, date, body, incoming(), partsKey());
    }
}
