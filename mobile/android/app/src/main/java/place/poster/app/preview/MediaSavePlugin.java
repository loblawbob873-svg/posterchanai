package place.poster.app.preview;

import android.util.Base64;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.ByteArrayInputStream;

/**
 * SAVE A PICTURE OR A VIDEO INTO THE PHONE'S GALLERY ("opening an image on android: first two buttons
 * share, do the same action"). The image viewer's Save went through saveBlobAs, which on the APK is the
 * share sheet -- the same thing its Share button does. This writes the bytes into MediaStore instead,
 * into ONE PosterChan tree with a subfolder per type: Download/PosterChan/{Images,Videos,Music,Voices,
 * Files} (MediaSaveRules). A voice is whatever the page SAYS is one (`hint: "voice"`).
 *
 * MediaStore with RELATIVE_PATH needs NO storage permission on Android 10+ (API 29). Below that a
 * public directory needs WRITE_EXTERNAL_STORAGE, which this app does not ask for, so `available`
 * says false there and the page keeps the share sheet as the only (and only labelled) option.
 */
@CapacitorPlugin(name = "MediaSave")
public final class MediaSavePlugin extends Plugin {
  private static final int MAX = (int) MediaSaveWriter.MAX;
  private static final int MAX_ENCODED = ((MAX + 2) / 3) * 4;

  @PluginMethod public void available(PluginCall call) {
    JSObject r = new JSObject(); r.put("ok", MediaSaveWriter.available()); call.resolve(r);
  }

  @PluginMethod public void save(PluginCall call) {
    final String encoded = call.getString("data", ""), mime = MediaSaveRules.mime(call.getString("mime", ""));
    final String name = call.getString("name", "");
    final String hint = call.getString("hint", "");
    if (!MediaSaveWriter.available()) { call.reject("saving to the gallery needs Android 10 or newer"); return; }
    new Thread(() -> {
      try {
        if (encoded.isEmpty() || encoded.length() > MAX_ENCODED) throw new IllegalArgumentException("file is empty or too large");
        byte[] bytes = Base64.decode(encoded, Base64.DEFAULT);
        if (bytes.length == 0 || bytes.length > MAX) throw new IllegalArgumentException("file is empty or too large");
        // The same write the native Messages screen uses (MediaSaveWriter): one folder tree, one set
        // of name rules, and a half-written row is taken back out on failure.
        String where = MediaSaveWriter.write(getContext().getContentResolver(),
            new ByteArrayInputStream(bytes), name, mime, hint);
        JSObject r = new JSObject();
        r.put("ok", true); r.put("where", where);
        call.resolve(r);
      } catch (Exception e) {
        call.reject(e.getMessage() == null ? "could not save" : e.getMessage(), e);
      }
    }, "pc-media-save").start();
  }
}
