package place.poster.app.sms;

/** Pure carrier-result reconciliation, kept Android-free so every mapping is executable in tests. */
final class MmsResult {
    static final int UNKNOWN = 0, SENT = 1, FAILED = 2;
    private MmsResult() { }

    static int classify(int result, int http, int providerBox) {
        /* The provider is durable and may be advanced by the system MMS service before our
         * PendingIntent runs. Never turn its Sent row back into an OEM code-0 warning. */
        if (providerBox == 2) return SENT;   // Telephony.Mms.MESSAGE_BOX_SENT
        if (providerBox == 5) return FAILED; // Telephony.Mms.MESSAGE_BOX_FAILED
        if (result == -1) return SENT;       // Activity.RESULT_OK
        /* A 2xx response proves the MMSC accepted the submission even if an OEM lost the Android
         * result code. This is send acceptance, not an optional recipient delivery receipt. */
        if (result == 0 && http >= 200 && http < 300) return SENT;
        if (result == 0) return UNKNOWN;
        return FAILED;
    }
}
