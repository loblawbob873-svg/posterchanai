package place.poster.app.weather;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * THE ONE THING THE WIDGET CANNOT WORK OUT FOR ITSELF: which PosterChan instance to ask.
 *
 * The launcher's process has no session and no localStorage, and the instance is chosen at runtime
 * by the person — one bundle serves every instance, so it cannot be baked in. So the client mirrors
 * it across, exactly as `PcThemePlugin` mirrors the theme and `CalendarPlugin` pushes the days: the
 * web side stays authoritative and this is a copy, written only from it.
 *
 * A STANDALONE INSTALL WRITES AN EMPTY BASE, and that is not a failure — it is the widget's
 * "PosterChan has no server to ask" state, which is a different sentence from "no location yet" and
 * from "no network". Weather is the one feature here that genuinely needs a server: the forecast is
 * proxied so that the upstream never sees a user (app/services/weather_service.py).
 */
@CapacitorPlugin(name = "Weather")
public class WeatherPlugin extends Plugin {

    @PluginMethod
    public void sync(PluginCall call) {
        WeatherStore.setServer(getContext(), call.getString("base", ""),
                                             call.getString("units", "metric"));
        // Redraw with whatever it now knows: a widget that has been sitting on "no server" since
        // the app was installed must not wait for the next hourly tick to notice one appeared.
        WeatherWidget.paint(getContext());
        call.resolve();
    }

    @PluginMethod
    public void status(PluginCall call) {
        JSObject o = new JSObject();
        o.put("hasServer", WeatherStore.hasServer(getContext()));
        o.put("hasPlace", WeatherStore.hasPlace(getContext()));
        o.put("place", WeatherStore.place(getContext()));
        o.put("at", WeatherStore.at(getContext()));
        call.resolve(o);
    }
}
