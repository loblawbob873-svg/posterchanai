package place.poster.app.preview;

/**
 * Where a saved file goes and what it is called -- pure, so a test can run it without Android.
 *
 * ONE FOLDER TREE, A SUBFOLDER PER TYPE: Download/PosterChan/{Images,Videos,Music,Voices,Files}.
 * Saves used to scatter across Pictures/PosterChan, Movies/PosterChan and Download/PosterChan -- three
 * places to look for "the thing I saved from PosterChan". The operator asked for one PosterChan folder;
 * a TOP-LEVEL /PosterChan is not writable through MediaStore on Android 10+ without
 * MANAGE_EXTERNAL_STORAGE (which this app does not ask for), so the tree lives under Download/, the one
 * standard directory the Downloads collection accepts ANY type into. The media scanner indexes it like
 * any other folder, so pictures, videos and songs saved there still show in gallery and music apps.
 *
 * A VOICE IS SAID, NOT GUESSED. A voice line is an ordinary audio/wav or an MP4 -- nothing in the bytes
 * or the MIME tells it from a song -- so the page passes a `hint` ("voice") when it knows, and only
 * then does a file go to Voices.
 */
public final class MediaSaveRules {
  private MediaSaveRules() { }

  public static final String ROOT = "Download/PosterChan/";

  /** A type the gallery understands, or a generic binary. Parameters (`; charset=`) are dropped. */
  public static String mime(String raw) {
    String m = raw == null ? "" : raw.split(";")[0].trim().toLowerCase(java.util.Locale.ROOT);
    return m.matches("[a-z]+/[a-z0-9.+-]+") ? m : "application/octet-stream";
  }

  public static String kind(String mime) { return kind(mime, ""); }

  /** image | video | audio | voice | file. `hint` is the page's word for what it is saving. */
  public static String kind(String mime, String hint) {
    if ("voice".equals(hint == null ? "" : hint.trim().toLowerCase(java.util.Locale.ROOT))) return "voice";
    String m = mime == null ? "" : mime;
    if (m.startsWith("image/")) return "image";
    if (m.startsWith("video/")) return "video";
    if (m.startsWith("audio/")) return "audio";
    return "file";
  }

  public static String folder(String kind) {
    if ("image".equals(kind)) return ROOT + "Images/";
    if ("video".equals(kind)) return ROOT + "Videos/";
    if ("audio".equals(kind)) return ROOT + "Music/";
    if ("voice".equals(kind)) return ROOT + "Voices/";
    return ROOT + "Files/";
  }

  /** A safe file name that ends in the right extension -- a gallery hides a picture saved as ".bin". */
  public static String name(String raw, String mime) {
    String s = (raw == null ? "" : raw).replaceAll("[^A-Za-z0-9._ -]", "_").trim();
    if (s.isEmpty() || ".".equals(s) || "..".equals(s)) s = "PosterChan-" + System.currentTimeMillis();
    if (s.length() > 120) s = s.substring(s.length() - 120);
    String ext = ext(mime);
    if (!ext.isEmpty() && !s.toLowerCase(java.util.Locale.ROOT).endsWith("." + ext)) {
      int dot = s.lastIndexOf('.');
      if (dot > 0 && s.length() - dot <= 5) s = s.substring(0, dot);
      s = s + "." + ext;
    }
    return s;
  }

  static String ext(String mime) {
    switch (mime) {
      case "image/jpeg": return "jpg";
      case "image/png": return "png";
      case "image/gif": return "gif";
      case "image/webp": return "webp";
      case "image/avif": return "avif";
      case "video/mp4": return "mp4";
      case "video/webm": return "webm";
      case "video/quicktime": return "mov";
      // A music app lists a file by its extension as much as by its type.
      case "audio/mpeg": case "audio/mp3": return "mp3";
      case "audio/mp4": case "audio/x-m4a": return "m4a";
      case "audio/ogg": return "ogg";
      case "audio/wav": case "audio/x-wav": case "audio/wave": return "wav";
      case "audio/flac": case "audio/x-flac": return "flac";
      case "audio/aac": return "aac";
      case "audio/opus": return "opus";
      default: return "";
    }
  }
}
