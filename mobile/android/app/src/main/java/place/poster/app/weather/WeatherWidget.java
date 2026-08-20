package place.poster.app.weather;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.widget.RemoteViews;

import place.poster.app.R;

/**
 * The weather, on the home screen. "i want the calendar widget and weather widget!"
 *
 * WHAT IT ASKS AND WHAT IT SENDS is stated in full in WeatherFetch: this user's own PosterChan
 * instance, a coordinate and a unit, and nothing else — no third party, no location permission, no
 * identifier. The place is typed, not sensed.
 *
 * IT NEVER GOES BLANK AND IT NEVER LIES. A failed refresh writes nothing, so what is on screen is
 * the last real reading with its age beside it once that age is worth mentioning (Weather.age). With
 * no place chosen it says "Tap to set your location" rather than drawing an empty box, and with a
 * place but no instance it says which of those two is missing — three different sentences because
 * they need three different things from the person reading them.
 *
 * THE REFRESH IS ON THE SYSTEM'S OWN TICK, NOT A TIMER OF OURS. `updatePeriodMillis` is the one
 * scheduled thing in this app's widgets, and it is here because a forecast is the one thing that
 * genuinely goes stale on its own — the calendar is pushed a month ahead and the music widget is
 * pushed on every change, so neither needs one. The platform clamps it to 30 minutes and batches it
 * with other wake-ups, and a tap refreshes immediately, which is what makes an hour acceptable.
 *
 * The fetch runs on a plain background thread, never on the broadcast's main thread: a receiver that
 * blocks is an ANR on the home screen, on somebody else's process.
 */
public class WeatherWidget extends AppWidgetProvider {

    /** Tap anywhere on the widget: refresh now, and open the app if there is nothing to show. */
    public static final String ACTION_TAP = "place.poster.app.WEATHER_TAP";

    @Override
    public void onUpdate(Context ctx, AppWidgetManager mgr, int[] ids) {
        for (int id : ids) mgr.updateAppWidget(id, build(ctx, mgr, id));
        refreshInBackground(ctx);
    }

    @Override
    public void onReceive(Context ctx, Intent intent) {
        super.onReceive(ctx, intent);
        if (intent == null || !ACTION_TAP.equals(intent.getAction())) return;
        if (!WeatherStore.hasPlace(ctx) || !WeatherStore.hasServer(ctx)) {
            openPicker(ctx);
            return;
        }
        refreshInBackground(ctx);
    }

    /** Redraw every instance of this widget. Called after a refresh lands. */
    public static void paint(Context ctx) {
        try {
            AppWidgetManager mgr = AppWidgetManager.getInstance(ctx);
            ComponentName me = new ComponentName(ctx, WeatherWidget.class);
            int[] ids = mgr.getAppWidgetIds(me);
            if (ids == null) return;
            for (int id : ids) mgr.updateAppWidget(id, build(ctx, mgr, id));
        } catch (Throwable ignored) { }
    }

    /**
     * Fetch again NOW and repaint when it lands. Used when the stored reading is in the wrong scale
     * — see WeatherPlugin.sync — because repainting a Celsius number after somebody asked for
     * Fahrenheit shows the same figure with the same sign and reads as a switch that does nothing.
     */
    public static void refreshNow(Context ctx) { refreshInBackground(ctx); }

    private static void refreshInBackground(final Context ctx) {
        final Context app = ctx.getApplicationContext();
        new Thread(new Runnable() {
            @Override public void run() {
                if (WeatherFetch.refresh(app)) paint(app);
            }
        }, "pcai-weather").start();
    }

    private static void openPicker(Context ctx) {
        try {
            Intent i = new Intent(ctx, WeatherConfigActivity.class)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            ctx.startActivity(i);
        } catch (Throwable ignored) { }
    }

    /**
     * ALSO CALLED WHEN A WIDGET IS RESIZED, so every instance is drawn for the box it is actually in.
     * Kept for callers that have no manager or id to hand.
     */
    static RemoteViews build(Context ctx) { return build(ctx, null, 0); }

    @Override
    public void onAppWidgetOptionsChanged(Context ctx, AppWidgetManager mgr, int id, Bundle opts) {
        super.onAppWidgetOptionsChanged(ctx, mgr, id, opts);
        // A RESIZE IS A REDRAW. Without this the widget keeps whatever proportions it was built with
        // and only catches up on the next hourly tick — so dragging it taller leaves the bands this
        // whole change is about, for an hour.
        try { mgr.updateAppWidget(id, build(ctx, mgr, id)); } catch (Throwable ignored) { }
    }

    /**
     * THE CARD FILLS ITS BOX; THE CONTENT DID NOT.
     *
     * "weather widget still has so much wasted space on top and bottom." The root is match_parent
     * with `gravity="center_vertical"`, so the background covers whatever the launcher gives it and
     * a fixed 46dp icon beside four short lines sits in the middle of it. At one cell that is snug;
     * at two it is a small blob with a band of empty card above and below, and nothing in a static
     * layout can know which it is — RemoteViews are inflated in the launcher's process with no
     * measure pass we ever see.
     *
     * So the size is ASKED FOR. `OPTION_APPWIDGET_MIN_HEIGHT` is what the launcher told the provider
     * (HomeActivity.onResized sets it, in dp), and the icon and the padding are scaled to it: a tall
     * widget gets a big glyph and looks deliberate instead of half-empty, a short one is unchanged.
     * `setViewLayoutHeight` is API 31, so below that it simply keeps the layout's own 46dp — an old
     * phone gets exactly what it got before rather than something broken.
     */
    static RemoteViews build(Context ctx, AppWidgetManager mgr, int id) {
        RemoteViews v = new RemoteViews(ctx.getPackageName(), R.layout.widget_weather);
        int boxDp = 0;
        try {
            if (mgr != null && id != 0) {
                Bundle o = mgr.getAppWidgetOptions(id);
                if (o != null) boxDp = o.getInt(AppWidgetManager.OPTION_APPWIDGET_MIN_HEIGHT, 0);
            }
        } catch (Throwable ignored) { }
        if (boxDp > 0 && Build.VERSION.SDK_INT >= 31) {
            // The card's own padding stays proportional so the glyph never touches the edge, and the
            // icon takes what is left. Clamped at both ends: below ~40dp it is unreadable, and past
            // ~120dp it stops looking like an icon and starts looking like a mistake.
            int pad = boxDp >= 150 ? 14 : 10;
            int side = boxDp - (pad * 2) - 6;
            side = side < 40 ? 40 : (side > 120 ? 120 : side);
            v.setViewLayoutWidth(R.id.ww_icon, side, android.util.TypedValue.COMPLEX_UNIT_DIP);
            v.setViewLayoutHeight(R.id.ww_icon, side, android.util.TypedValue.COMPLEX_UNIT_DIP);
            int padPx = (int) (pad * ctx.getResources().getDisplayMetrics().density);
            v.setViewPadding(R.id.ww_body, padPx, padPx, padPx, padPx);
        }
        boolean place = WeatherStore.hasPlace(ctx), server = WeatherStore.hasServer(ctx);
        Double temp = WeatherStore.temp(ctx);
        long at = WeatherStore.at(ctx);

        if (!Weather.haveReading(at, temp)) {
            int why = Weather.whyEmpty(place, server);
            v.setTextViewText(R.id.ww_temp, "—");
            v.setTextViewText(R.id.ww_desc, ctx.getString(
                    why == Weather.NEED_PLACE ? R.string.weather_need_place
                  : why == Weather.NEED_SERVER ? R.string.weather_need_server
                  : R.string.weather_need_network));
            v.setTextViewText(R.id.ww_place, WeatherStore.place(ctx));
            v.setTextViewText(R.id.ww_range, "");
            v.setViewVisibility(R.id.ww_place, place ? View.VISIBLE : View.GONE);
            v.setImageViewResource(R.id.ww_icon, R.drawable.ic_wx_cloud);
        } else {
            int code = WeatherStore.code(ctx);
            boolean day = WeatherStore.day(ctx);
            String suffix = WeatherStore.unitSuffix(ctx);
            v.setTextViewText(R.id.ww_temp, Weather.temp(temp, suffix));
            String age = Weather.age(at, System.currentTimeMillis());
            String desc = Weather.describe(code, day);
            v.setTextViewText(R.id.ww_desc, age.isEmpty() ? desc : desc + "   ·   " + age);
            v.setTextViewText(R.id.ww_place, WeatherStore.place(ctx));
            v.setViewVisibility(R.id.ww_place, View.VISIBLE);
            v.setTextViewText(R.id.ww_range,
                    Weather.range(WeatherStore.max(ctx), WeatherStore.min(ctx), suffix));
            v.setImageViewResource(R.id.ww_icon, iconRes(Weather.icon(code, day)));
        }

        // FLAG_IMMUTABLE or Android 12 throws when the notification/widget is built — the same rule
        // MusicWidget's transport buttons follow.
        int flags = PendingIntent.FLAG_UPDATE_CURRENT;
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M) flags |= PendingIntent.FLAG_IMMUTABLE;
        Intent tap = new Intent(ctx, WeatherWidget.class).setAction(ACTION_TAP);
        v.setOnClickPendingIntent(R.id.ww_body,
                PendingIntent.getBroadcast(ctx, 0, tap, flags));
        return v;
    }

    /**
     * Name to resource, explicitly. `getIdentifier` would do it in one line and is exactly the call
     * that resolves to 0 under resource shrinking, giving a widget with no icon and nothing in any
     * log to say why.
     */
    static int iconRes(String name) {
        if ("sun".equals(name)) return R.drawable.ic_wx_sun;
        if ("moon".equals(name)) return R.drawable.ic_wx_moon;
        if ("fog".equals(name)) return R.drawable.ic_wx_fog;
        if ("drizzle".equals(name)) return R.drawable.ic_wx_drizzle;
        if ("rain".equals(name)) return R.drawable.ic_wx_rain;
        if ("snow".equals(name)) return R.drawable.ic_wx_snow;
        if ("storm".equals(name)) return R.drawable.ic_wx_storm;
        return R.drawable.ic_wx_cloud;
    }
}
