package place.poster.app.preview;

/** Where a saved file goes and what it is called -- pure, so a test can run it without Android. */
public final class MediaSaveRules {
  private MediaSaveRules() { }

  /** A type the gallery understands, or a generic binary. Parameters (`; charset=`) are dropped. */
  public static String mime(String raw) {
    String m = raw == null ? "" : raw.split(";")[0].trim().toLowerCase(java.util.Locale.ROOT);
    return m.matches("[a-z]+/[a-z0-9.+-]+") ? m : "application/octet-stream";
  }

  public static String kind(String mime) {
    if (mime.startsWith("image/")) return "image";
    if (mime.startsWith("video/")) return "video";
    return "file";
  }

  public static String folder(String kind) {
    if ("image".equals(kind)) return "Pictures/PosterChan/";
    if ("video".equals(kind)) return "Movies/PosterChan/";
    return "Download/PosterChan/";
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
      default: return "";
    }
  }
}
