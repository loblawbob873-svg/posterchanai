package place.poster.app.sms;

/** Archive pages order provider rows by timestamp AND id; a timestamp alone loses ties. */
public final class SmsArchiveCursor {
    private SmsArchiveCursor() { }
    public static String date(boolean mms) {
        return mms ? "(CASE WHEN date>100000000000 THEN date ELSE date*1000 END)" : "date";
    }
    /* THE ARGUMENTS ARE TEXT AND MUST BE READ AS NUMBERS. ContentResolver binds every selection
     * argument as a STRING. SQLite converts it for a comparison with a column that has INTEGER
     * affinity -- which is why SMS (`date>?`) always worked -- but NOT for a comparison with an
     * EXPRESSION, and the MMS side compares the seconds-to-milliseconds CASE expression. There an
     * integer is always LESS than any text, so `expr > '1790700000000'` was false for every row and
     * the native sweep read NO picture message at all from Sep 15 (when this cursor arrived) on --
     * sent or received, old or new, while its report said "0 picture messages" and nothing failed.
     * ("i sent my mom a picture today and its not in texts also".) CAST makes both sides numbers. */
    public static String where(boolean mms) {
        String date = date(mms);
        return "(" + date + ">CAST(? AS INTEGER) OR (" + date + "=CAST(? AS INTEGER) AND _id>CAST(? AS INTEGER)))";
    }
    public static String order(boolean mms) { return date(mms) + " ASC, _id ASC"; }
    public static String[] args(long date, long id) {
        return new String[]{Long.toString(Math.max(0, date)), Long.toString(Math.max(0, date)), Long.toString(id)};
    }
}
