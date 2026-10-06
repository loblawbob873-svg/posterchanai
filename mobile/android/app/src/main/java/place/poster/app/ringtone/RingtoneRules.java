package place.poster.app.ringtone;

/**
 * The decisions behind "Set as phone ringtone", kept free of Android so tests can run them with plain
 * javac: which file name a ringtone is stored under, and what the page is told afterwards.
 */
public final class RingtoneRules {
  private RingtoneRules() {}

  /** Ringtones live here, so Android's own ringtone picker lists them too. */
  public static final String FOLDER = "Ringtones/";

  /** A plain file name: letters, digits, dash, underscore and one extension; never a path. */
  public static String name(String requested, String mime) {
    String ext = "audio/mpeg".equals(mime) ? ".mp3" : ".ogg";
    String base = requested == null ? "" : requested.replaceAll("\\.[A-Za-z0-9]{1,4}$", "");
    base = base.replaceAll("[^A-Za-z0-9_-]", "-").replaceAll("-{2,}", "-").replaceAll("^-|-$", "");
    if (base.isEmpty()) base = "posterchan-ringtone";
    if (base.length() > 60) base = base.substring(0, 60);
    return base + ext;
  }

  /** Only audio is accepted; anything else is stored as Ogg (the bundled ringtone's format). */
  public static String mime(String m) {
    return "audio/mpeg".equals(m) ? "audio/mpeg" : "audio/ogg";
  }

  /**
   * What the page is told: saved (it is in Ringtones/ and the picker), set (it is the default), and
   * whether the person still has to allow "Modify system settings" -- Android's rule for changing the
   * default ringtone, which no app can grant itself.
   */
  public static String outcome(boolean saved, boolean canWrite, boolean wantDefault) {
    if (!saved) return "failed";
    if (!wantDefault) return "saved";
    return canWrite ? "set" : "needs-permission";
  }
}
