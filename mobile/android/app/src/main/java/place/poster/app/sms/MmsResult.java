package place.poster.app.sms;

/** Pure carrier-result reconciliation, kept Android-free so every mapping is executable in tests. */
final class MmsResult {
    static final int UNKNOWN = 0, SENT = 1, FAILED = 2;
    private MmsResult() { }

    static int classify(int result, int http, int providerBox) {
        /* The provider is durable and may be advanced by the system MMS service before our
         * PendingIntent runs. Never turn its Sent row back into an OEM code-0 warning. */
        if (providerBox == 2) return SENT;   // Telephony.Mms.MESSAGE_BOX_SENT
        // Android can report transport RESULT_OK after SendConf parsing marked this row failed.
        // Never overwrite a provider rejection with transport success or an ambiguous callback.
        if (providerBox == 5) return FAILED; // Telephony.Mms.MESSAGE_BOX_FAILED
        if (result == -1) return SENT;       // Activity.RESULT_OK, with no contrary provider result.
        // IO_ERROR has also been observed after delivery. Without positive acceptance evidence,
        // neither it nor code zero (even with HTTP 2xx) proves success or failure of submission.
        if (result == 0 || result == 5) return UNKNOWN;
        return FAILED;
    }
}
