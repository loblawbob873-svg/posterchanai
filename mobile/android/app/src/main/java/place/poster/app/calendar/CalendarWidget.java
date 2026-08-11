package place.poster.app.calendar;

import android.app.PendingIntent;
import android.appwidget.AppWidgetManager;
import android.appwidget.AppWidgetProvider;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Build;
import android.view.View;
import android.widget.RemoteViews;

import org.json.JSONArray;
import org.json.JSONObject;

import java.text.SimpleDateFormat;
import java.util.Calendar;
import java.util.Locale;

import place.poster.app.MainActivity;
import place.poster.app.R;

/**
 * Today's events, on the home screen.
 *
 * WHERE THE DATA COMES FROM, and why it is a cache. A calendar item here is an encrypted Nostr
 * document; the widget is drawn by the LAUNCHER's process, which has no key, no network session and
 * no business having either. So the WebView pushes what it already decrypted (CalendarPlugin.push)
 * and this renders it — exactly the split the music widget uses, for exactly the same reason.
 *
 * IT IS PUSHED SEVERAL DAYS AHEAD, not just today. If it only held today's events it would be wrong
 * from the first midnight after the app was last opened — and a calendar widget that is confidently
 * wrong is worse than one that says it is stale. Holding a week means the widget keeps telling the
 * truth for a week without the app being opened at all, and it re-reads which day is "today" on every
 * draw (including the daily ACTION_DATE_CHANGED broadcast) rather than trusting when it was written.
 *
 * `updatePeriodMillis` is 0: there is nothing to poll for. What changes the display is either a push
 * from the app or the date rolling over, and both arrive as broadcasts.
 */
public class CalendarWidget extends AppWidgetProvider {

  static final String PREFS = "pcai_calendar";
  static final String KEY_DAYS = "days";      // {"YYYY-MM-DD": [{"t":"09:00","s":"Title"}, …]}
  static final String KEY_AT = "at";

  private static final int[] ROWS = { R.id.cw_1, R.id.cw_2, R.id.cw_3, R.id.cw_4 };
  /** How far ahead the widget looks to fill its rows, and therefore how long it keeps telling the
   *  truth with the app never opened. A MONTH, not a week: the push is one pass over data the client
   *  has already decrypted and a few KB of preferences, while the cost of it running out is a widget
   *  that goes blank on a phone whose owner has not opened the app since the holidays. The app asks
   *  for this number rather than carrying its own copy (CalendarPlugin.window). */
  static final int WINDOW_DAYS = 31;

  @Override
  public void onUpdate(Context ctx, AppWidgetManager mgr, int[] ids) {
    for (int id : ids) mgr.updateAppWidget(id, build(ctx));
  }

  @Override
  public void onReceive(Context ctx, Intent intent) {
    super.onReceive(ctx, intent);
    String a = intent != null ? intent.getAction() : null;
    // MIDNIGHT. Without this the widget shows yesterday's list until something else happens to
    // redraw it, which on a home screen can be hours — and "today" being yesterday is the single
    // most confusing thing a calendar can do.
    if (Intent.ACTION_DATE_CHANGED.equals(a) || Intent.ACTION_TIME_CHANGED.equals(a)
        || Intent.ACTION_TIMEZONE_CHANGED.equals(a) || Intent.ACTION_BOOT_COMPLETED.equals(a)) {
      refresh(ctx);
    }
  }

  /** Redraw every placed instance. Safe to call from anywhere, including with none placed. */
  public static void refresh(Context ctx) {
    try {
      AppWidgetManager mgr = AppWidgetManager.getInstance(ctx);
      int[] ids = mgr.getAppWidgetIds(new ComponentName(ctx, CalendarWidget.class));
      if (ids == null || ids.length == 0) return;
      RemoteViews v = build(ctx);
      for (int id : ids) mgr.updateAppWidget(id, v);
    } catch (Throwable ignored) {
      // A widget that cannot be drawn must never take the app down with it.
    }
  }

  private static RemoteViews build(Context ctx) {
    RemoteViews v = new RemoteViews(ctx.getPackageName(), R.layout.widget_calendar);

    Calendar now = Calendar.getInstance();
    v.setTextViewText(R.id.cw_day, String.valueOf(now.get(Calendar.DAY_OF_MONTH)));
    v.setTextViewText(R.id.cw_date,
        new SimpleDateFormat("EEEE, MMMM", Locale.getDefault()).format(now.getTime()));

    SharedPreferences p = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    String raw = p.getString(KEY_DAYS, "");
    boolean known = raw != null && raw.length() > 0;

    /* TODAY FIRST, THEN THE DAYS AFTER IT — the same rule the desktop widget follows, and for the
     * same reason: an empty day is the COMMON case, and "nothing on today" by itself is less use
     * than the thing that is actually coming. Today's events fill the rows; whatever is left over is
     * filled from the following days, labelled with the weekday so a later row can never be mistaken
     * for one of today's. */
    int shown = 0, more = 0;
    Calendar cur = (Calendar) now.clone();
    for (int d = 0; d < WINDOW_DAYS; d++) {
      if (d > 0) cur.add(Calendar.DAY_OF_YEAR, 1);
      String k = new SimpleDateFormat("yyyy-MM-dd", Locale.US).format(cur.getTime());
      JSONArray list = null;
      try {
        if (raw != null && raw.length() > 0) list = new JSONObject(raw).optJSONArray(k);
      } catch (Throwable ignored) {
      }
      if (list == null) continue;
      String dayLabel = d == 0 ? "" :
          new SimpleDateFormat("EEE", Locale.getDefault()).format(cur.getTime());
      for (int i = 0; i < list.length(); i++) {
        if (shown >= ROWS.length) { more++; continue; }
        JSONObject e = list.optJSONObject(i);
        String when = e != null ? e.optString("t", "") : "";
        String what = e != null ? e.optString("s", "") : "";
        String left = d == 0 ? when : (dayLabel + (when.isEmpty() ? "" : " " + when));
        v.setTextViewText(ROWS[shown], (left.isEmpty() ? "" : left + "   ") + what);
        // A finished appointment is DIMMED, not dropped — a day whose entries disappear as it goes
        // on reads as a calendar losing things. RemoteViews can set a colour; it cannot set alpha on
        // a TextView on every platform, so this is two literal colours.
        boolean past = d == 0 && e != null && e.optBoolean("p", false);
        v.setTextColor(ROWS[shown], past ? 0xFF5E7385 : 0xFFE8FBFF);
        v.setViewVisibility(ROWS[shown], View.VISIBLE);
        shown++;
      }
    }
    for (int i = shown; i < ROWS.length; i++) v.setViewVisibility(ROWS[i], View.GONE);

    if (more > 0) {
      v.setTextViewText(R.id.cw_more, "+ " + more + " more");
      v.setViewVisibility(R.id.cw_more, View.VISIBLE);
    } else {
      v.setViewVisibility(R.id.cw_more, View.GONE);
    }

    // THREE STATES, NOT TWO. "Nothing on" and "this widget has never been given anything" look
    // identical if both draw an empty box — and the second one is the one a person should act on.
    if (shown == 0) {
      v.setTextViewText(R.id.cw_empty,
          known ? "Nothing coming up."
                : "Open PosterChan to load your calendar.");
      v.setViewVisibility(R.id.cw_empty, View.VISIBLE);
    } else {
      v.setViewVisibility(R.id.cw_empty, View.GONE);
    }

    int f = PendingIntent.FLAG_UPDATE_CURRENT
        | (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M ? PendingIntent.FLAG_IMMUTABLE : 0);
    Intent open = new Intent(ctx, MainActivity.class)
        .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_CLEAR_TOP)
        .putExtra(CalendarPlugin.EXTRA_OPEN_CALENDAR, true);
    v.setOnClickPendingIntent(R.id.cw_body,
        PendingIntent.getActivity(ctx, 7, open, f));
    return v;
  }
}
