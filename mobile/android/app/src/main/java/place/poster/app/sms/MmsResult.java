package place.poster.app.sms;

/** Pure carrier-result reconciliation, kept Android-free so every mapping is executable in tests. */
final class MmsResult {
    static final int UNKNOWN = 0, SENT = 1, FAILED = 2;
    private MmsResult() { }

    static int classify(int result, int http, int providerBox) {
        /* The provider is durable and may be advanced by the system MMS service before our
         * PendingIntent runs. Never turn its Sent row back into an OEM code-0 warning. */
        if (providerBox == 2) return SENT;   // Telephony.Mms.MESSAGE_BOX_SENT
        if (result == -1) return SENT;       // Activity.RESULT_OK can arrive after an earlier failure.
        // IO_ERROR has been observed after a picture reached its recipient. Without a carrier
        // confirmation we cannot distinguish that from a failure before transmission. Preserve
        // uncertainty, including old rows this app marked failed, and never invite a blind retry.
        if (result == 5) return UNKNOWN;
        if (providerBox == 5) return FAILED; // Telephony.Mms.MESSAGE_BOX_FAILED
        /* A 2xx response proves the MMSC accepted the submission even if an OEM lost the Android
         * result code. This is send acceptance, not an optional recipient delivery receipt. */
        if (result == 0 && http >= 200 && http < 300) return SENT;
        if (result == 0) return UNKNOWN;
        return FAILED;
    }
}
