package place.poster.app.preview;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.net.Uri;
import android.os.Build;
import android.provider.MediaStore;

import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * THE ONE WRITE INTO THE PHONE'S DOWNLOADS/PosterChan TREE, shared by the WebView's MediaSave plugin and
 * the native Messages screen ("sms apk does not let me save media attachments" -- ThreadActivity had no
 * Save at all). One implementation, so a picture saved from a text lands in the same folder, under the
 * same name rules, as one saved from the image viewer.
 *
 * STREAMED, not handed a byte[]: an MMS video read off `content://mms/part` goes straight into
 * MediaStore without a second whole-file copy in memory. Bounded by MAX all the same.
 *
 * MediaStore with RELATIVE_PATH needs NO storage permission on Android 10+ (API 29). Below that a public
 * directory needs WRITE_EXTERNAL_STORAGE, which this app does not ask for -- `available` says false and
 * callers offer the share sheet instead.
 */
public final class MediaSaveWriter {
  private MediaSaveWriter() { }

  public static final long MAX = 200L * 1024 * 1024;

  public static boolean available() { return Build.VERSION.SDK_INT >= 29; }

  /** Writes `in` (not closed here) and answers where it went, e.g. "Download/PosterChan/Images/a.jpg". */
  public static String write(ContentResolver cr, InputStream in, String rawName, String rawMime, String hint)
      throws IOException {
    if (Build.VERSION.SDK_INT < 29) throw new IOException("saving to the phone needs Android 10 or newer");
    if (in == null) throw new IOException("the file could not be opened");
    final String mime = MediaSaveRules.mime(rawMime);
    final String name = MediaSaveRules.name(rawName, mime);
    final String kind = MediaSaveRules.kind(mime, hint);
    // ONE tree: every type goes into the Downloads collection, the only one that accepts any MIME
    // under Download/ (Images/Video/Audio refuse a RELATIVE_PATH outside their own top folders).
    // The scanner still files the rows by type, so the gallery and music apps list them.
    ContentValues v = new ContentValues();
    v.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
    v.put(MediaStore.MediaColumns.MIME_TYPE, mime);
    v.put(MediaStore.MediaColumns.RELATIVE_PATH, MediaSaveRules.folder(kind));
    v.put(MediaStore.MediaColumns.IS_PENDING, 1);
    Uri uri = cr.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v);
    if (uri == null) throw new IOException("the gallery refused the file");
    try {
      long total = 0;
      try (OutputStream out = cr.openOutputStream(uri)) {
        if (out == null) throw new IOException("could not write the file");
        byte[] buf = new byte[64 * 1024];
        int n;
        while ((n = in.read(buf)) >= 0) {
          if (n == 0) continue;
          total += n;
          if (total > MAX) throw new IOException("file is too large to save");
          out.write(buf, 0, n);
        }
      }
      if (total == 0) throw new IOException("file is empty");
      ContentValues done = new ContentValues();
      done.put(MediaStore.MediaColumns.IS_PENDING, 0);
      cr.update(uri, done, null, null);
      return MediaSaveRules.folder(kind) + name;
    } catch (IOException | RuntimeException e) {
      // A half-written row would sit in the gallery as a broken file; take it back out.
      try { cr.delete(uri, null, null); } catch (RuntimeException ignored) { }
      throw e;
    }
  }
}
