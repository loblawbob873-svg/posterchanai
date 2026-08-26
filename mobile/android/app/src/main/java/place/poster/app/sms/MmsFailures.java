package place.poster.app.sms;

import android.content.Context;

/** Durable carrier failure details keyed by the phone provider's MMS row id. */
final class MmsFailures {
    private static final String PREF = "poster_mms_failures";
    private MmsFailures() { }

    static void put(Context ctx, long id, int code, int http) {
        if (ctx == null || id <= 0) return;
        ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE).edit()
                .putString(String.valueOf(id), reason(code, http)).apply();
    }

    static String get(Context ctx, long id) {
        if (ctx == null || id <= 0) return "";
        return ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE)
                .getString(String.valueOf(id), "");
    }

    static void clear(Context ctx, long id) {
        if (ctx != null && id > 0) ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE).edit()
                .remove(String.valueOf(id)).apply();
    }

    /* SmsManager's MMS result values. Kept numeric so this remains installable on API 26, where
       several newer named constants do not exist at runtime. */
    static String reason(int code, int http) {
        String why;
        switch (code) {
            case 2: why = "invalid carrier APN"; break;
            case 3: why = "could not connect to the MMS network"; break;
            case 4: why = "carrier server rejected it"; break;
            case 5: why = "could not read or send the attachment"; break;
            case 6: why = "carrier requested a retry"; break;
            case 7: why = "carrier MMS configuration is invalid"; break;
            case 8: why = "mobile data is unavailable"; break;
            case 9: why = "the selected SIM is invalid"; break;
            case 10: why = "the selected SIM is inactive"; break;
            case 11: why = "mobile data is disabled"; break;
            case 12: why = "no default messages app is available"; break;
            default: why = "carrier send failed (code " + code + ")";
        }
        return http > 0 ? why + ", HTTP " + http : why;
    }
}
