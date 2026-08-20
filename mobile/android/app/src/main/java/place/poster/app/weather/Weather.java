package place.poster.app.weather;

/**
 * EVERYTHING HUMAN ABOUT A FORECAST, with no Android in it.
 *
 * The numbers come from this node's own `/api/weather`, which is Open-Meteo proxied and cached (see
 * app/services/weather_service.py). Turning a WMO code into a word, a reading into a line of text
 * and a timestamp into "2h ago" is arithmetic and string work, so it lives here and
 * `tests/test_android_weather.py` RUNS it rather than grepping for it.
 *
 * THE GROUPING IS TRANSCRIBED FROM THE CLIENT'S `_wxDesc` (static/js/client/os.js), deliberately:
 * the desktop widget and the phone widget describing the same sky in different words is the kind of
 * difference nobody reports and everybody notices. The client's glyphs are emoji, which are banned
 * from this app's Android UI strings (tests/test_android_icon_sprite.py), so the code maps to a
 * drawable NAME here and the words are identical.
 */
public final class Weather {

    /** How old a reading may be before the widget says so instead of presenting it as current. */
    public static final long STALE_AFTER_MS = 3L * 60 * 60 * 1000;

    private Weather() { }

    /** The word for a WMO code. Grouped, not enumerated — "slight" versus "moderate" drizzle does
     *  not survive being drawn at 13sp, and pretending to that precision is worse than not. */
    public static String describe(int code, boolean day) {
        if (code == 0) return day ? "Clear" : "Clear night";
        if (code <= 2) return "Mostly clear";
        if (code == 3) return "Overcast";
        if (code <= 48) return "Fog";
        if (code <= 57) return "Drizzle";
        if (code <= 67) return "Rain";
        if (code <= 77) return "Snow";
        if (code <= 82) return "Showers";
        if (code <= 86) return "Snow showers";
        return "Thunderstorm";
    }

    /** The drawable base name for a code — `ic_wx_<this>` in res/drawable. */
    public static String icon(int code, boolean day) {
        if (code <= 2) return day ? "sun" : "moon";
        if (code == 3) return "cloud";
        if (code <= 48) return "fog";
        if (code <= 57) return "drizzle";
        if (code <= 67) return "rain";
        if (code <= 77) return "snow";
        if (code <= 82) return "rain";
        if (code <= 86) return "snow";
        return "storm";
    }

    /**
     * A temperature as it is shown. NULL-SAFE ON PURPOSE: a reading that arrived without a
     * temperature must draw an em dash, never "null" and never 0°, which is a real temperature and
     * would be a confident lie in exactly the weather where it matters.
     */
    public static String temp(Double c, String unit) {
        if (c == null || c.isNaN() || c.isInfinite()) return "—";
        return Math.round(c) + (unit == null || unit.isEmpty() ? "°" : unit);
    }

    /** "H 12°  L 3°", or as much of it as the day actually reported. */
    public static String range(Double max, Double min, String unit) {
        String h = temp(max, unit), l = temp(min, unit);
        if ("—".equals(h) && "—".equals(l)) return "";
        return "H " + h + "   L " + l;
    }

    /**
     * HOW OLD THE READING IS, and this is the line that keeps the widget honest.
     *
     * With no network the widget draws the last reading it has rather than going blank — a blank
     * widget reads as broken, and the temperature an hour ago is nearly always the answer somebody
     * wanted. But a reading from Tuesday presented as now is worse than either, so anything past
     * STALE_AFTER_MS is labelled and nothing under it is: a timestamp on a fresh reading is noise
     * that trains people to ignore the one time it matters.
     */
    public static String age(long readingAtMs, long nowMs) {
        long d = nowMs - readingAtMs;
        if (readingAtMs <= 0) return "";
        if (d < STALE_AFTER_MS) return "";
        long hours = d / (60L * 60 * 1000);
        if (hours < 48) return hours + "h ago";
        return (hours / 24) + "d ago";
    }

    /** Whether what we hold is worth drawing at all, as opposed to "tap to set your location". */
    public static boolean haveReading(long readingAtMs, Double temp) {
        return readingAtMs > 0 && temp != null && !temp.isNaN();
    }

    /**
     * THE THREE THINGS THE WIDGET CAN SAY WHEN IT HAS NO NUMBER, and they are different sentences
     * because they need different actions from the person looking at it.
     */
    public static final int NEED_PLACE = 0;      // no location has ever been chosen
    public static final int NEED_SERVER = 1;     // a place, but no PosterChan instance to ask
    public static final int NEED_NETWORK = 2;    // both, and nothing has come back yet

    public static int whyEmpty(boolean hasPlace, boolean hasServer) {
        if (!hasPlace) return NEED_PLACE;
        if (!hasServer) return NEED_SERVER;
        return NEED_NETWORK;
    }
}
