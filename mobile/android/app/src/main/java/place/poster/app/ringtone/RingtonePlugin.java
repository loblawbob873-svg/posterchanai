package place.poster.app.ringtone;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Intent;
import android.database.Cursor;
import android.media.RingtoneManager;
import android.net.Uri;
import android.os.Build;
import android.provider.MediaStore;
import android.provider.Settings;
import android.util.Base64;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.OutputStream;

/**
 * SET THE POSTERCHAN RINGTONE ON THE PHONE ("we need to make it easy somehow for the android launcher
 * users to set that" / "maybe User Settings -> Ring Tone"). One tap in User Settings → Ringtone:
 *
 *  1. the sound is written into MediaStore under Ringtones/ as a RINGTONE, so Android's own picker lists
 *     it from then on -- and a second tap replaces that same entry instead of adding a copy;
 *  2. it is made the default ringtone with RingtoneManager -- which Android only allows an app the person
 *     has given "Modify system settings". Without it, this opens that screen for this app and says so; the
 *     page's next tap then finishes the job. Nothing is changed silently.
 *
 * MediaStore with RELATIVE_PATH needs no storage permission on Android 10+ (API 29); below that
 * `available` says false and the page offers the file to download instead.
 */
@CapacitorPlugin(name = "Ringtone")
public final class RingtonePlugin extends Plugin {
  private static final int MAX = 5 * 1024 * 1024;

  @PluginMethod public void available(PluginCall call) {
    JSObject r = new JSObject();
    r.put("ok", Build.VERSION.SDK_INT >= 29);
    r.put("canWrite", Build.VERSION.SDK_INT >= 23 && Settings.System.canWrite(getContext()));
    r.put("appAlert", place.poster.app.push.PushEventService.alertSoundOn(getContext()));
    call.resolve(r);
  }

  /**
   * "PosterChan notifications play the PosterChan Alert" -- the switch in User Settings → Sounds. No
   * permission is needed: it only picks which of PosterChan's own two message channels a notification goes to
   * (the alert one carries the bundled res/raw/posterchan_alert sound). The first notification after turning
   * it on is the proof, so `test:true` posts one straight away.
   */
  @PluginMethod public void appAlert(PluginCall call) {
    boolean on = Boolean.TRUE.equals(call.getBoolean("on", false));
    place.poster.app.push.PushEventService.setAlertSound(getContext(), on);
    if (Boolean.TRUE.equals(call.getBoolean("test", false))) {
      place.poster.app.push.PushEventService.show(getContext(), "PosterChan", on ? "This is the PosterChan Alert ♪" : "Notifications use your phone's sound again", "msg", "pc-alert-test", "notifications");
    }
    JSObject r = new JSObject(); r.put("ok", true); r.put("appAlert", on); call.resolve(r);
  }

  @PluginMethod public void install(PluginCall call) {
    final String encoded = call.getString("data", "");
    final String mime = RingtoneRules.mime(call.getString("mime", ""));
    final String name = RingtoneRules.name(call.getString("name", ""), mime);
    final String title = call.getString("title", "PosterChan");
    final boolean wantDefault = Boolean.TRUE.equals(call.getBoolean("setDefault", true));
    // "notification" = the phone's notification sound (Notifications/, TYPE_NOTIFICATION); else a ringtone.
    final String kind = call.getString("kind", "ringtone");
    final boolean notify = RingtoneRules.isNotification(kind);
    final String folder = RingtoneRules.folder(kind);
    if (Build.VERSION.SDK_INT < 29) { call.reject("setting a ringtone needs Android 10 or newer"); return; }
    new Thread(() -> {
      ContentResolver cr = getContext().getContentResolver();
      Uri uri = null; boolean created = false;
      try {
        byte[] bytes = Base64.decode(encoded, Base64.DEFAULT);
        if (bytes.length == 0 || bytes.length > MAX) throw new IllegalArgumentException("the sound is empty or too large");
        Uri collection = MediaStore.Audio.Media.EXTERNAL_CONTENT_URI;
        try (Cursor c = cr.query(collection, new String[]{MediaStore.MediaColumns._ID},
            MediaStore.MediaColumns.DISPLAY_NAME + "=? AND " + MediaStore.MediaColumns.RELATIVE_PATH + "=?",
            new String[]{name, folder}, null)) {
          if (c != null && c.moveToFirst()) uri = Uri.withAppendedPath(collection, String.valueOf(c.getLong(0)));
        }
        if (uri == null) {
          ContentValues v = new ContentValues();
          v.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
          v.put(MediaStore.MediaColumns.TITLE, title);
          v.put(MediaStore.MediaColumns.MIME_TYPE, mime);
          v.put(MediaStore.MediaColumns.RELATIVE_PATH, folder);
          v.put(MediaStore.Audio.Media.IS_RINGTONE, notify ? 0 : 1);
          v.put(MediaStore.Audio.Media.IS_NOTIFICATION, notify ? 1 : 0);
          v.put(MediaStore.Audio.Media.IS_ALARM, 0);
          v.put(MediaStore.Audio.Media.IS_MUSIC, 0);
          v.put(MediaStore.MediaColumns.IS_PENDING, 1);
          uri = cr.insert(collection, v);
          created = true;
          if (uri == null) throw new java.io.IOException("Android refused the ringtone file");
        }
        try (OutputStream out = cr.openOutputStream(uri, "wt")) {
          if (out == null) throw new java.io.IOException("could not write the ringtone");
          out.write(bytes);
        }
        if (created) { ContentValues done = new ContentValues(); done.put(MediaStore.MediaColumns.IS_PENDING, 0); cr.update(uri, done, null, null); }
        boolean canWrite = Settings.System.canWrite(getContext());
        String outcome = RingtoneRules.outcome(true, canWrite, wantDefault);
        if ("set".equals(outcome)) {
          RingtoneManager.setActualDefaultRingtoneUri(getContext(), notify ? RingtoneManager.TYPE_NOTIFICATION : RingtoneManager.TYPE_RINGTONE, uri);
        } else if ("needs-permission".equals(outcome)) {
          Intent i = new Intent(Settings.ACTION_MANAGE_WRITE_SETTINGS, Uri.parse("package:" + getContext().getPackageName()));
          i.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
          getContext().startActivity(i);
        }
        JSObject r = new JSObject();
        r.put("ok", true); r.put("outcome", outcome); r.put("uri", uri.toString()); r.put("where", folder + name);
        call.resolve(r);
      } catch (Exception e) {
        if (created && uri != null) { try { cr.delete(uri, null, null); } catch (Exception ignored) { } }
        call.reject(e.getMessage() == null ? "could not set the ringtone" : e.getMessage(), e);
      }
    }, "pc-ringtone").start();
  }
}
