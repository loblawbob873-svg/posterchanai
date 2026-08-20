package place.poster.app.weather;

import android.content.Context;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.net.URLEncoder;

/**
 * WHERE THE WEATHER COMES FROM, AND WHAT LEAVES THE PHONE.
 *
 * It asks THIS USER'S OWN PosterChan instance — `<base>/api/weather` — and nothing else. It does not
 * contact Open-Meteo, or any other third party, ever. The instance is the address the client is
 * already signed in to; with none stored the widget says so and makes no request at all.
 *
 * What that request carries: a latitude, a longitude and "metric" or "imperial". No account, no
 * device id, no token — the endpoint is a public read (app/routers/weather.py). The node then asks
 * Open-Meteo from its own IP with the coordinate ROUNDED TO ABOUT A KILOMETRE and caches the answer
 * for ten minutes, so the upstream sees one server and a grid square rather than a person and a
 * street (app/services/weather_service.py). That is the same path the desktop's weather widget
 * already takes; this widget adds no new destination to the app.
 *
 * NO LOCATION PERMISSION IS INVOLVED. The place is TYPED, looked up by name through the same node's
 * `/api/weather/geocode`, so the phone's own location is never read and the widget never has to ask
 * for it. That is a deliberate trade: a permission prompt for a home-screen widget is a bad bargain,
 * and a place somebody chose is a better answer than a fix from a cold GPS anyway.
 *
 * WITH NO NETWORK IT MAKES NO CLAIM. A failed request writes nothing, so WeatherWidget draws the
 * last real reading with its age beside it (Weather.age) rather than a blank box or a stale number
 * presented as now.
 */
public final class WeatherFetch {

    private static final String TAG = "PosterChan";
    private static final int TIMEOUT_MS = 12000;
    /** Bounded because it is parsed in the launcher's process; a forecast is a couple of KB. */
    private static final int MAX_BYTES = 256 * 1024;

    private WeatherFetch() { }

    /** True when a fresh reading was stored. False for every failure, having changed nothing. */
    public static boolean refresh(Context ctx) {
        String base = WeatherStore.base(ctx);
        if (base.isEmpty() || !WeatherStore.hasPlace(ctx)) return false;
        String url = base + "/api/weather?lat=" + WeatherStore.lat(ctx)
                   + "&lon=" + WeatherStore.lon(ctx)
                   + "&units=" + WeatherStore.units(ctx);
        String body = get(url);
        if (body == null) return false;
        try {
            JSONObject o = new JSONObject(body);
            if (!o.optBoolean("ok", false)) return false;
            JSONObject now = o.optJSONObject("now");
            if (now == null || now.isNull("temp")) return false;
            JSONObject units = o.optJSONObject("units");
            JSONArray days = o.optJSONArray("days");
            JSONObject today = days != null && days.length() > 0 ? days.optJSONObject(0) : null;
            WeatherStore.setReading(ctx,
                    now.optDouble("temp"),
                    now.isNull("code") ? null : now.optInt("code"),
                    now.optBoolean("day", true),
                    today == null || today.isNull("max") ? null : today.optDouble("max"),
                    today == null || today.isNull("min") ? null : today.optDouble("min"),
                    units == null ? "°" : units.optString("temp", "°"),
                    System.currentTimeMillis());
            return true;
        } catch (Throwable t) {
            Log.w(TAG, "weather: could not read the forecast", t);
            return false;
        }
    }

    /** City search for the picker: [{name, lat, lon}], newest style of the same endpoint. */
    public static JSONArray search(Context ctx, String query) {
        String base = WeatherStore.base(ctx);
        if (base.isEmpty() || query == null || query.trim().isEmpty()) return new JSONArray();
        String url;
        try {
            url = base + "/api/weather/geocode?q=" + URLEncoder.encode(query.trim(), "UTF-8");
        } catch (Throwable t) { return new JSONArray(); }
        String body = get(url);
        if (body == null) return new JSONArray();
        try {
            JSONArray r = new JSONObject(body).optJSONArray("results");
            return r == null ? new JSONArray() : r;
        } catch (Throwable t) { return new JSONArray(); }
    }

    private static String get(String url) {
        HttpURLConnection c = null;
        try {
            c = (HttpURLConnection) new URL(url).openConnection();
            c.setConnectTimeout(TIMEOUT_MS);
            c.setReadTimeout(TIMEOUT_MS);
            c.setInstanceFollowRedirects(true);
            c.setRequestProperty("Accept", "application/json");
            if (c.getResponseCode() / 100 != 2) return null;
            InputStream is = c.getInputStream();
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n, total = 0;
            while ((n = is.read(buf)) > 0) {
                total += n;
                if (total > MAX_BYTES) return null;
                out.write(buf, 0, n);
            }
            return out.toString("UTF-8");
        } catch (Throwable t) {
            // Ordinary and expected: no signal, an instance that is down, a phone in a tunnel.
            return null;
        } finally {
            if (c != null) try { c.disconnect(); } catch (Throwable ignored) { }
        }
    }
}
