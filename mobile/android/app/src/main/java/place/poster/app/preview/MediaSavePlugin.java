package place.poster.app.preview;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.MediaStore;
import android.util.Base64;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.OutputStream;

/**
 * SAVE A PICTURE OR A VIDEO INTO THE PHONE'S GALLERY ("opening an image on android: first two buttons
 * share, do the same action"). The image viewer's Save went through saveBlobAs, which on the APK is the
 * share sheet -- the same thing its Share button does. This writes the bytes into MediaStore instead:
 * images to Pictures/PosterChan, video to Movies/PosterChan, anything else to Download/PosterChan.
 *
 * MediaStore with RELATIVE_PATH needs NO storage permission on Android 10+ (API 29). Below that a
 * public directory needs WRITE_EXTERNAL_STORAGE, which this app does not ask for, so `available`
 * says false there and the page keeps the share sheet as the only (and only labelled) option.
 */
@CapacitorPlugin(name = "MediaSave")
public final class MediaSavePlugin extends Plugin {
  private static final int MAX = 200 * 1024 * 1024;
  private static final int MAX_ENCODED = ((MAX + 2) / 3) * 4;

  @PluginMethod public void available(PluginCall call) {
    JSObject r = new JSObject(); r.put("ok", Build.VERSION.SDK_INT >= 29); call.resolve(r);
  }

  @PluginMethod public void save(PluginCall call) {
    final String encoded = call.getString("data", ""), mime = MediaSaveRules.mime(call.getString("mime", ""));
    final String name = MediaSaveRules.name(call.getString("name", ""), mime);
    if (Build.VERSION.SDK_INT < 29) { call.reject("saving to the gallery needs Android 10 or newer"); return; }
    new Thread(() -> {
      Uri uri = null;
      ContentResolver cr = getContext().getContentResolver();
      try {
        if (encoded.isEmpty() || encoded.length() > MAX_ENCODED) throw new IllegalArgumentException("file is empty or too large");
        byte[] bytes = Base64.decode(encoded, Base64.DEFAULT);
        if (bytes.length == 0 || bytes.length > MAX) throw new IllegalArgumentException("file is empty or too large");
        String kind = MediaSaveRules.kind(mime);
        Uri collection = "image".equals(kind) ? MediaStore.Images.Media.EXTERNAL_CONTENT_URI
            : "video".equals(kind) ? MediaStore.Video.Media.EXTERNAL_CONTENT_URI
            : MediaStore.Downloads.EXTERNAL_CONTENT_URI;
        ContentValues v = new ContentValues();
        v.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
        v.put(MediaStore.MediaColumns.MIME_TYPE, mime);
        v.put(MediaStore.MediaColumns.RELATIVE_PATH, MediaSaveRules.folder(kind));
        v.put(MediaStore.MediaColumns.IS_PENDING, 1);
        uri = cr.insert(collection, v);
        if (uri == null) throw new java.io.IOException("the gallery refused the file");
        try (OutputStream out = cr.openOutputStream(uri)) {
          if (out == null) throw new java.io.IOException("could not write the file");
          out.write(bytes);
        }
        ContentValues done = new ContentValues(); done.put(MediaStore.MediaColumns.IS_PENDING, 0);
        cr.update(uri, done, null, null);
        JSObject r = new JSObject();
        r.put("ok", true); r.put("uri", uri.toString()); r.put("where", MediaSaveRules.folder(kind) + name);
        call.resolve(r);
      } catch (Exception e) {
        if (uri != null) { try { cr.delete(uri, null, null); } catch (Exception ignored) { } }
        call.reject(e.getMessage() == null ? "could not save" : e.getMessage(), e);
      }
    }, "pc-media-save").start();
  }
}
