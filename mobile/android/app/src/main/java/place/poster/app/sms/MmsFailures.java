package place.poster.app.sms;

import android.content.Context;

/** Durable carrier failure details keyed by the phone provider's MMS row id. */
final class MmsFailures {
    private static final String PREF = "poster_mms_failures";
    private MmsFailures() { }

    static void put(Context ctx, long id, int code, int http) {
        if (ctx == null || id <= 0) return;
        ctx.getSharedPreferences(PREF, Context.MODE_PRIVATE).edit()
                .putString(String.valueOf(id), reason(ctx, code, http)).apply();
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
            case 0: why = "Android cancelled MMS before the carrier returned a reason"; break;
            case 1: why = "carrier MMS transport failed without a reason"; break;
            default: why = "carrier send failed (code " + code + ")";
        }
        return http > 0 ? why + ", HTTP " + http : why;
    }

    /** Add facts the handset can prove when Android collapses the real error into result code 0. */
    static String reason(Context ctx, int code, int http) {
        String why = reason(code, http);
        if (code != 0 || ctx == null) return why;
        try {
            if (!HasRole.sms(ctx)) return why + "; PosterChan is not the active Messages app";
        } catch (Throwable ignored) { }
        try {
            if (android.os.Build.VERSION.SDK_INT >= 23
                    && ctx.checkSelfPermission(android.Manifest.permission.SEND_SMS)
                       != android.content.pm.PackageManager.PERMISSION_GRANTED)
                return why + "; SMS permission is not granted";
        } catch (Throwable ignored) { }
        try {
            android.telephony.TelephonyManager tm = (android.telephony.TelephonyManager)
                    ctx.getSystemService(Context.TELEPHONY_SERVICE);
            if (tm != null && tm.getSimState() != android.telephony.TelephonyManager.SIM_STATE_READY)
                return why + "; the SIM is not ready";
        } catch (Throwable ignored) { }
        int sub = android.telephony.SubscriptionManager.getDefaultSmsSubscriptionId();
        if (sub == android.telephony.SubscriptionManager.INVALID_SUBSCRIPTION_ID)
            return why + "; Android has no default SMS SIM";
        return why + "; verify mobile data and the carrier MMS APN for SIM " + sub;
    }
}
