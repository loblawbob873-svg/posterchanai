package place.poster.app.weather;

import android.content.Context;
import android.content.SharedPreferences;

/**
 * What the weather widget knows, between one draw and the next.
 *
 * SharedPreferences for the same reason the launcher's arrangement is: this is per-DEVICE state
 * about a widget the LAUNCHER draws, in a process with no key, no session and no business having
 * either. Nothing here is private beyond a place name and a coordinate the person typed.
 *
 * `base` is the PosterChan instance the client is signed in to, mirrored across by WeatherPlugin.
 * It is the only address this widget ever contacts — see WeatherFetch for what that means.
 */
public final class WeatherStore {

    public static final String PREFS = "pcai_weather";

    private static final String K_BASE = "base";
    private static final String K_UNITS = "units";
    private static final String K_LAT = "lat";
    private static final String K_LON = "lon";
    private static final String K_PLACE = "place";
    private static final String K_TEMP = "temp";
    private static final String K_CODE = "code";
    private static final String K_DAY = "day";
    private static final String K_MAX = "max";
    private static final String K_MIN = "min";
    private static final String K_UNIT_SUFFIX = "usuffix";
    private static final String K_AT = "at";

    private WeatherStore() { }

    static SharedPreferences sp(Context c) {
        return c.getApplicationContext().getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    public static String base(Context c) { return sp(c).getString(K_BASE, ""); }
    public static String units(Context c) { return sp(c).getString(K_UNITS, "metric"); }
    public static String place(Context c) { return sp(c).getString(K_PLACE, ""); }
    public static double lat(Context c) { return dbl(sp(c).getString(K_LAT, "")); }
    public static double lon(Context c) { return dbl(sp(c).getString(K_LON, "")); }
    public static boolean hasPlace(Context c) { return !sp(c).getString(K_LAT, "").isEmpty(); }
    public static boolean hasServer(Context c) { return !base(c).isEmpty(); }
    public static long at(Context c) { return sp(c).getLong(K_AT, 0); }

    /** The last reading, or null where the field never arrived — see Weather.temp on why not 0. */
    public static Double temp(Context c) { return num(sp(c).getString(K_TEMP, "")); }
    public static Double max(Context c) { return num(sp(c).getString(K_MAX, "")); }
    public static Double min(Context c) { return num(sp(c).getString(K_MIN, "")); }
    public static int code(Context c) { return sp(c).getInt(K_CODE, 0); }
    public static boolean day(Context c) { return sp(c).getBoolean(K_DAY, true); }
    public static String unitSuffix(Context c) { return sp(c).getString(K_UNIT_SUFFIX, "°"); }

    /** The instance to ask, and in what units. Written by the client, never guessed at here. */
    public static void setServer(Context c, String base, String units) {
        sp(c).edit().putString(K_BASE, base == null ? "" : base.trim())
                    .putString(K_UNITS, "imperial".equals(units) ? "imperial" : "metric")
                    .apply();
    }

    public static void setPlace(Context c, double lat, double lon, String name) {
        sp(c).edit().putString(K_LAT, String.valueOf(lat))
                    .putString(K_LON, String.valueOf(lon))
                    .putString(K_PLACE, name == null ? "" : name)
                    .apply();
    }

    /**
     * A READING IS ONLY EVER REPLACED BY A READING. A failed fetch writes nothing at all, so the
     * widget keeps the last true number and says how old it is — which is the whole reason it does
     * not go blank on a train.
     */
    public static void setReading(Context c, Double temp, Integer code, Boolean day,
                                  Double max, Double min, String unitSuffix, long atMs) {
        if (temp == null) return;
        sp(c).edit().putString(K_TEMP, String.valueOf(temp))
                    .putInt(K_CODE, code == null ? 0 : code)
                    .putBoolean(K_DAY, day == null || day)
                    .putString(K_MAX, max == null ? "" : String.valueOf(max))
                    .putString(K_MIN, min == null ? "" : String.valueOf(min))
                    .putString(K_UNIT_SUFFIX, unitSuffix == null || unitSuffix.isEmpty() ? "°" : unitSuffix)
                    .putLong(K_AT, atMs)
                    .apply();
    }

    private static Double num(String s) {
        if (s == null || s.isEmpty()) return null;
        try { return Double.valueOf(s); } catch (Throwable t) { return null; }
    }

    private static double dbl(String s) {
        Double d = num(s);
        return d == null ? 0 : d;
    }
}
