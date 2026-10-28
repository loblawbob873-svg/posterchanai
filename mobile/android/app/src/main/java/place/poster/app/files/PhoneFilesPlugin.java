package place.poster.app.files;

import android.Manifest;
import android.content.ClipData;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.os.storage.StorageManager;
import android.os.storage.StorageVolume;
import android.provider.Settings;
import android.util.Base64;

import androidx.core.content.FileProvider;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.File;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Files' "This phone": the phone's shared storage, through PhoneFilesCore (which confines every path to a storage
 * root). The client side is phonefiles.js, which presents this with the desktop bridge's shape for hostfiles.js.
 *
 * ACCESS IS ASKED FOR, NEVER ASSUMED. Android 11+ grants a file manager "All files access" on a Settings screen the
 * person turns on themselves (MANAGE_EXTERNAL_STORAGE); 8-10 use the storage runtime permission. Until then `access`
 * answers false and the screen says so with a button, rather than showing an empty phone.
 */
@CapacitorPlugin(name = "PhoneFiles")
public final class PhoneFilesPlugin extends Plugin {
  private static final int REQ = 7311;

  private boolean granted() {
    if (Build.VERSION.SDK_INT >= 30) return Environment.isExternalStorageManager();
    return getContext().checkSelfPermission(Manifest.permission.READ_EXTERNAL_STORAGE)
        == PackageManager.PERMISSION_GRANTED;
  }

  /** Internal shared storage first, then every mounted removable volume (SD card, USB stick). */
  private List<Map.Entry<File, String>> volumes() {
    Context c = getContext();
    List<Map.Entry<File, String>> out = new ArrayList<>();
    File internal = Environment.getExternalStorageDirectory();
    if (internal != null) out.add(new java.util.AbstractMap.SimpleEntry<>(internal, "Internal storage"));
    StorageManager sm = (StorageManager) c.getSystemService(Context.STORAGE_SERVICE);
    File[] dirs = c.getExternalFilesDirs(null);
    if (dirs != null) for (File d : dirs) {
      if (d == null) continue;
      String p = d.getPath();
      int cut = p.indexOf("/Android/data/");
      if (cut <= 0) continue;
      File root = new File(p.substring(0, cut));
      if (internal != null && root.getPath().equals(internal.getPath())) continue;
      String label = "Storage";
      try {
        StorageVolume v = sm == null ? null : sm.getStorageVolume(root);
        if (v != null) label = v.getDescription(c);
      } catch (Exception ignored) { }
      out.add(new java.util.AbstractMap.SimpleEntry<>(root, label));
    }
    return out;
  }

  private PhoneFilesCore core() {
    List<File> roots = new ArrayList<>();
    for (Map.Entry<File, String> v : volumes()) roots.add(v.getKey());
    return new PhoneFilesCore(roots);
  }

  private static JSObject json(Map<String, Object> m) {
    JSObject o = new JSObject();
    for (Map.Entry<String, Object> e : m.entrySet()) {
      Object v = e.getValue();
      if (v instanceof List) {
        JSArray a = new JSArray();
        for (Object x : (List<?>) v) a.put(x instanceof Map ? json(cast(x)) : x);
        o.put(e.getKey(), a);
      } else o.put(e.getKey(), v == null ? org.json.JSONObject.NULL : v);
    }
    return o;
  }

  @SuppressWarnings("unchecked")
  private static Map<String, Object> cast(Object o) { return (Map<String, Object>) o; }

  private void fail(PluginCall call, Exception e) {
    call.reject(e.getMessage() == null ? "could not do that" : e.getMessage());
  }

  @PluginMethod public void access(PluginCall call) {
    JSObject r = new JSObject();
    r.put("granted", granted());
    r.put("api", Build.VERSION.SDK_INT);
    call.resolve(r);
  }

  /** Opens the system screen (11+) or the permission prompt (8-10). The client re-asks `access` when it comes back. */
  @PluginMethod public void requestAccess(PluginCall call) {
    try {
      if (Build.VERSION.SDK_INT >= 30) {
        Intent i = new Intent(Settings.ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION,
            Uri.parse("package:" + getContext().getPackageName()));
        try { getActivity().startActivity(i); }
        catch (Exception e) { getActivity().startActivity(new Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION)); }
      } else {
        getActivity().requestPermissions(new String[]{Manifest.permission.READ_EXTERNAL_STORAGE}, REQ);
      }
      JSObject r = new JSObject(); r.put("opened", true); call.resolve(r);
    } catch (Exception e) { fail(call, e); }
  }

  @PluginMethod public void roots(PluginCall call) {
    JSArray a = new JSArray();
    boolean first = true;
    for (Map.Entry<File, String> v : volumes()) {
      JSObject o = new JSObject();
      o.put("name", v.getValue()); o.put("path", v.getKey().getPath()); o.put("kind", first ? "home" : "mount");
      a.put(o); first = false;
    }
    JSObject r = new JSObject(); r.put("roots", a); call.resolve(r);
  }

  @PluginMethod public void list(PluginCall call) {
    try { call.resolve(json(core().list(call.getString("dir", "")))); } catch (Exception e) { fail(call, e); }
  }

  @PluginMethod public void read(PluginCall call) {
    try {
      PhoneFilesCore c = core();
      String path = call.getString("path", "");
      byte[] b = c.read(path, call.getLong("max", 0L));
      File f = c.inside(path);
      JSObject r = new JSObject(); r.put("b64", Base64.encodeToString(b, Base64.NO_WRAP));
      r.put("path", f.getPath()); r.put("size", b.length); r.put("mtime", f.lastModified()); call.resolve(r);
    } catch (Exception e) { fail(call, e); }
  }

  @PluginMethod public void mkdir(PluginCall call) {
    try { JSObject r = new JSObject(); r.put("path", core().mkdir(call.getString("dir", ""), call.getString("name", ""))); call.resolve(r); }
    catch (Exception e) { fail(call, e); }
  }

  @PluginMethod public void rename(PluginCall call) {
    try { JSObject r = new JSObject(); r.put("path", core().rename(call.getString("from", ""), call.getString("to", ""))); call.resolve(r); }
    catch (Exception e) { fail(call, e); }
  }

  @PluginMethod public void trash(PluginCall call) {
    try {
      String from = call.getString("target", "");
      JSObject r = new JSObject(); r.put("trashed", core().trash(from)); r.put("from", from); call.resolve(r);
    } catch (Exception e) { fail(call, e); }
  }

  @PluginMethod public void transfer(PluginCall call) {
    try {
      List<String> items = new ArrayList<>();
      JSArray a = call.getArray("items", new JSArray());
      for (int i = 0; i < a.length(); i++) items.add(a.getString(i));
      JSArray done = new JSArray();
      for (String[] x : core().transfer(items, call.getString("destination", ""), call.getBoolean("move", false))) {
        JSObject o = new JSObject(); o.put("from", x[0]); o.put("to", x[1]); done.put(o);
      }
      JSObject r = new JSObject(); r.put("moved", call.getBoolean("move", false)); r.put("items", done); call.resolve(r);
    } catch (Exception e) { fail(call, e); }
  }

  @PluginMethod public void writeBytes(PluginCall call) {
    try {
      String b64 = call.getString("b64", "");
      if (b64.length() > ((PhoneFilesCore.WRITE_MAX + 2) / 3) * 4) throw new Exception("that file is too big to save here");
      byte[] data = Base64.decode(b64, Base64.DEFAULT);
      call.resolve(json(core().writeBytes(call.getString("path", ""), data, call.getLong("mtime", 0L))));
    } catch (Exception e) { fail(call, e); }
  }

  /** "Open with": the phone's own app for that kind of file, through the FileProvider (external-path "."). */
  @PluginMethod public void open(PluginCall call) {
    final String path = call.getString("path", "");
    getActivity().runOnUiThread(() -> {
      try {
        File f = core().inside(path);
        if (!f.isFile()) throw new Exception("that is not a file");
        Uri uri = FileProvider.getUriForFile(getContext(), getContext().getPackageName() + ".fileprovider", f);
        Intent view = new Intent(Intent.ACTION_VIEW).setDataAndType(uri, PhoneFilesCore.mimeOf(f.getName()));
        view.setClipData(ClipData.newRawUri(f.getName(), uri));
        view.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        if (view.resolveActivity(getContext().getPackageManager()) == null) throw new Exception("no app on this phone opens that");
        getActivity().startActivity(Intent.createChooser(view, "Open " + f.getName()));
        JSObject r = new JSObject(); r.put("ok", true); call.resolve(r);
      } catch (Exception e) {
        JSObject r = new JSObject(); r.put("ok", false); r.put("why", e.getMessage() == null ? "could not open it" : e.getMessage());
        call.resolve(r);
      }
    });
  }
}
