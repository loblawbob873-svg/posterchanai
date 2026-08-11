package place.poster.app.calendar;

import android.content.Context;
import android.content.SharedPreferences;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * The WebView's end of the home-screen calendar widget (see CalendarWidget for the split).
 *
 * One direction only, and one method that matters: the client hands over the days it has already
 * decrypted, this stores them, the widget draws them. Nothing here parses iCalendar, expands a
 * recurrence rule or talks to a relay — that all happens once, in the client, where the code that
 * does it already exists and is tested. A second implementation in Java is how the widget would end
 * up disagreeing with the app about what day something is on.
 */
@CapacitorPlugin(name = "CalendarWidget")
public class CalendarPlugin extends Plugin {

  /** Set on the launch Intent when the widget is tapped; the client reads it once and opens Calendar. */
  public static final String EXTRA_OPEN_CALENDAR = "pc_open_calendar";

  /**
   * Store the next few days and redraw.
   *
   * `days` is `{"YYYY-MM-DD": [{"t":"09:00","s":"Title","p":true}, …]}` — the local date as the key,
   * because the widget decides which day is "today" AT DRAW TIME rather than trusting when this was
   * written. That is what keeps it right through midnight, and through the app not being opened for
   * a week.
   */
  @PluginMethod
  public void push(PluginCall call) {
    JSObject days = call.getObject("days");
    SharedPreferences.Editor e = getContext()
        .getSharedPreferences(CalendarWidget.PREFS, Context.MODE_PRIVATE).edit();
    e.putString(CalendarWidget.KEY_DAYS, days != null ? days.toString() : "");
    e.putLong(CalendarWidget.KEY_AT, System.currentTimeMillis());
    e.apply();
    CalendarWidget.refresh(getContext());
    call.resolve();
  }

  /** How far ahead to send, so the client and the widget cannot disagree about the window. */
  @PluginMethod
  public void window(PluginCall call) {
    JSObject out = new JSObject();
    out.put("days", CalendarWidget.WINDOW_DAYS);
    call.resolve(out);
  }

  /**
   * Did the app open because the widget was tapped? Consumed, so re-checking on every resume cannot
   * replay it — the same rule the music widget's launch action follows.
   */
  @PluginMethod
  public void consumeLaunch(PluginCall call) {
    boolean want = false;
    try {
      if (getActivity() != null && getActivity().getIntent() != null) {
        want = getActivity().getIntent().getBooleanExtra(EXTRA_OPEN_CALENDAR, false);
        if (want) getActivity().getIntent().removeExtra(EXTRA_OPEN_CALENDAR);
      }
    } catch (Throwable ignored) {
    }
    JSObject out = new JSObject();
    out.put("open", want);
    call.resolve(out);
  }
}
