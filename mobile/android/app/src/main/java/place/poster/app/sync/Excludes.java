package place.poster.app.sync;

import java.util.Calendar;
import java.util.List;
import java.util.Locale;
import java.util.TimeZone;

/**
 * The cheap half of exclusions, plus the trash's date naming.
 *
 * THIS IS DELIBERATELY A STRICT SUBSET OF THE JAVASCRIPT MATCHER, and that is the whole design.
 * foldersync.js `excluder()` is the authority — it decides what syncs, it is what the desktop uses,
 * and it is covered by tests. A second full implementation in Java would drift, and the direction it
 * drifts in is catastrophic: if Java excluded something JS did not, the scan would omit paths the
 * engine still has in `base`, the engine would read them as "deleted here", and it would delete them
 * from every other device. A folder exclusion must never be able to delete anything.
 *
 * So Java only skips what it is CERTAIN JavaScript would also skip: a path exactly equal to a
 * pattern that has no wildcards in it. That is enough for the case that matters — not walking
 * Pictures/Old and its twenty thousand photos on every sweep — and everything subtler (globs,
 * anchoring, `**`) is left to the engine, which filters the manifest anyway. Missing a skip here
 * costs some directory reads. Adding one costs data.
 */
final class Excludes {
  private Excludes() {}

  static boolean matches(String rel, List<String> patterns) {
    if (rel == null || patterns == null || patterns.isEmpty()) return false;
    for (String raw : patterns) {
      if (raw == null) continue;
      String p = raw.trim().replace('\\', '/');
      while (p.endsWith("/")) p = p.substring(0, p.length() - 1);
      if (p.startsWith("/")) p = p.substring(1);
      // Anything clever is JavaScript's job — see the class comment.
      if (p.isEmpty() || p.contains("*") || p.contains("?")) continue;
      if (rel.equals(p) || rel.startsWith(p + "/")) return true;
      // A bare folder name matches at any depth, which is what someone means by typing "Old".
      if (!p.contains("/")) {
        if (rel.endsWith("/" + p) || rel.contains("/" + p + "/")) return true;
      }
    }
    return false;
  }

  /**
   * Files a writer has announced as in-flight by NAME. The substitute for locking: on Android there
   * is no lock to take through SAF at all, and a camera still writing a photo or another sync app
   * mid-copy both leave these behind.
   */
  static boolean isTempName(String name) {
    if (name == null) return false;
    String n = name.toLowerCase(Locale.US);
    if (n.startsWith("~$") || n.startsWith(".~lock.")) return true;
    return n.endsWith(".crdownload") || n.endsWith(".part") || n.endsWith(".partial")
        || n.endsWith(".tmp") || n.endsWith(".temp") || n.endsWith(".swp") || n.endsWith(".download");
  }

  /** `2026-08-09`, UTC — the same name desktop/fsbridge.js gives a trash day, so a folder synced
   *  between a phone and a laptop has ONE trash layout rather than two. */
  static String dayName(long millis) {
    Calendar c = Calendar.getInstance(TimeZone.getTimeZone("UTC"));
    c.setTimeInMillis(millis);
    return String.format(Locale.US, "%04d-%02d-%02d",
        c.get(Calendar.YEAR), c.get(Calendar.MONTH) + 1, c.get(Calendar.DAY_OF_MONTH));
  }

  /** The inverse, for expiring trash days. 0 when the name is not one of ours — which is why an
   *  unrecognised directory in .pc-trash is left alone rather than deleted. */
  static long dayMillis(String name) {
    if (name == null || name.length() != 10) return 0;
    try {
      Calendar c = Calendar.getInstance(TimeZone.getTimeZone("UTC"));
      c.clear();
      c.set(Integer.parseInt(name.substring(0, 4)),
            Integer.parseInt(name.substring(5, 7)) - 1,
            Integer.parseInt(name.substring(8, 10)));
      return c.getTimeInMillis();
    } catch (Exception e) { return 0; }
  }
}
