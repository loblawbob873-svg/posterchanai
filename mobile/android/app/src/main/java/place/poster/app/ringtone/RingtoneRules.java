package place.poster.app.ringtone;

/**
 * The decisions behind "Set as phone ringtone", kept free of Android so tests can run them with plain
 * javac: which file name a ringtone is stored under, and what the page is told afterwards.
 */
public final class RingtoneRules {
  private RingtoneRules() {}

  /** Ringtones live here, so Android's own ringtone picker lists them too. */
  public static final String FOLDER = "Ringtones/";
  /** Notification sounds live here, so Android's notification-sound picker lists them. */
  public static final String NOTIFY_FOLDER = "Notifications/";

  /** "notification" stores a notification sound; anything else is a ringtone (the original behaviour). */
  public static boolean isNotification(String kind) { return "notification".equals(kind); }

  public static String folder(String kind) { return isNotification(kind) ? NOTIFY_FOLDER : FOLDER; }

  /**
   * The channel PosterChan's message notifications post to. A channel's SOUND is fixed when Android creates
   * it -- an app can never change it afterwards -- so "PosterChan notifications play the PosterChan Alert" is
   * a second channel that carries the sound, and the switch picks between the two.
   */
  public static String messagesChannel(boolean alertSound) { return alertSound ? "pcai_messages_alert" : "pcai_messages"; }

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
