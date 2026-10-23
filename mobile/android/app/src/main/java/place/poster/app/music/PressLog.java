package place.poster.app.music;

import java.util.ArrayList;
import java.util.List;

/**
 * The last few transport presses, for the "Music & car controls" line in Settings -> Phone.
 *
 * Pure (no Android types) so a javac harness can run it. A car button that "does nothing at all" can be
 * any of four different failures -- the press never reached this app, it reached the STANDBY session,
 * it reached the real session and the page never answered, or it was answered and performed -- and from
 * the driver's seat they are identical. Each entry records which one it was: what was pressed, which
 * surface/session delivered it, and what came of it. Mirrored to logcat under MusicService.TAG.
 *
 * Static and bounded (MAX entries): the case it explains is often the cold one, whose service ends in
 * stopSelf(), so it cannot live on an instance; and it must never grow.
 */
public final class PressLog {
  public static final int MAX = 10;

  public static final class Entry {
    public final long at;          // System.currentTimeMillis()
    public final String action;    // play / pause / next / prev / ...
    public final String via;       // which surface or session delivered it
    public volatile String outcome;// pending / answered / unanswered / ...
    Entry(long at, String action, String via, String outcome) {
      this.at = at; this.action = action; this.via = via; this.outcome = outcome;
    }
    @Override public String toString() { return action + " via " + via + ": " + outcome; }
  }

  private static final ArrayList<Entry> entries = new ArrayList<>();

  /** Record a press; returns its entry so the receipt check can settle it later. */
  public static synchronized Entry add(long at, String action, String via, String outcome) {
    Entry e = new Entry(at, s(action), s(via), s(outcome));
    entries.add(e);
    while (entries.size() > MAX) entries.remove(0);
    return e;
  }

  public static void settle(Entry e, String outcome) { if (e != null) e.outcome = s(outcome); }

  /** Newest first. */
  public static synchronized List<Entry> recent() {
    ArrayList<Entry> out = new ArrayList<>(entries);
    java.util.Collections.reverse(out);
    return out;
  }

  public static synchronized void clear() { entries.clear(); }

  private static String s(String v) { return v == null ? "" : v; }
}
