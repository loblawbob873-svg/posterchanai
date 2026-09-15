package place.poster.app.sms;

/** Archive pages order provider rows by timestamp AND id; a timestamp alone loses ties. */
public final class SmsArchiveCursor {
    private SmsArchiveCursor() { }
    public static String date(boolean mms) {
        return mms ? "(CASE WHEN date>100000000000 THEN date ELSE date*1000 END)" : "date";
    }
    public static String where(boolean mms) {
        String date = date(mms);
        return "(" + date + ">? OR (" + date + "=? AND _id>?))";
    }
    public static String order(boolean mms) { return date(mms) + " ASC, _id ASC"; }
    public static String[] args(long date, long id) {
        return new String[]{Long.toString(Math.max(0, date)), Long.toString(Math.max(0, date)), Long.toString(id)};
    }
}
