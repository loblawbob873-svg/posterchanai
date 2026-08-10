package place.poster.app.sync;

import android.app.Activity;
import android.content.ContentResolver;
import android.content.Context;
import android.content.Intent;
import android.database.Cursor;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.BatteryManager;
import android.os.Build;
import android.provider.DocumentsContract;
import android.util.Base64;

import androidx.activity.result.ActivityResult;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.ActivityCallback;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.InputStream;
import java.io.OutputStream;
import java.security.MessageDigest;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;

/**
 * Folder sync on Android — the SAF end of the same two interfaces the desktop bridge implements
 * (see desktop/fsbridge.js). The decision engine and the executor are shared JavaScript; this is
 * only I/O, and the JS shim maps it onto `window.pcFs` so sync.js cannot tell the two apart.
 *
 * SAF, NOT java.io.File. Scoped storage means the app has no path-level access to Pictures or
 * Documents at all; what it can have is a TREE URI the user granted in the system picker, made
 * durable with takePersistableUriPermission so it survives a reboot and an app update. That grant IS
 * the confinement — there is no ".." to defend against here, because there are no paths, only
 * document ids inside a tree the user chose.
 *
 * DO NOT USE DocumentFile. It is the obvious API and it is unusable at this scale: DocumentFile
 * .listFiles() issues a query per child and each getName()/length()/lastModified() is another
 * round trip through the content provider, so a Pictures folder of 20k photos becomes tens of
 * thousands of IPCs. The cursor below asks for every column it needs, for a whole directory, in one
 * query — the difference between a sweep and a phone that gets hot and never finishes.
 *
 * SAF CANNOT SET A FILE'S LAST-MODIFIED TIME. There is no writable COLUMN_LAST_MODIFIED, so the
 * desktop trick of stamping a download with the source mtime is impossible here. That would make
 * every downloaded file look locally-edited on the next sweep — except that write() returns the
 * mtime the provider actually gave the file, and the executor records THAT as the agreed state
 * (syncrun.js `agree(path, {sha, size, mtime: st.mtime})`). The loop closes because the executor
 * believes the filesystem rather than its own intent.
 *
 * THERE IS NO WATCHER, AND NO BACKGROUND SYNC YET. SAF exposes no reliable change notification for
 * a tree, and polling one is precisely the battery bug the policy exists to avoid, so watch() answers
 * false. Today that means the app syncs when it is OPEN — when you press the button, and when the
 * app starts.
 *
 * A WorkManager job is the intended next step and its constraints map one-for-one onto the sync
 * policy (setRequiresCharging / NetworkType.UNMETERED / setRequiresBatteryNotLow), which is the OS
 * holding the work until it is cheap rather than the app asking repeatedly. What makes it more than
 * a few lines is that the encryption key lives in the WebView: a native worker can walk the tree and
 * hash it cheaply, but it cannot encrypt or upload without either a headless WebView or moving the
 * crypto into Java. Until that is decided, claiming background sync here would be a lie.
 */
@CapacitorPlugin(name = "FolderSync")
public class FolderSyncPlugin extends Plugin {

  private static final String PART = ".pcpart";
  private static final String TRASH = ".pc-trash";
  private static final String[] COLS = {
      DocumentsContract.Document.COLUMN_DOCUMENT_ID,
      DocumentsContract.Document.COLUMN_DISPLAY_NAME,
      DocumentsContract.Document.COLUMN_MIME_TYPE,
      DocumentsContract.Document.COLUMN_SIZE,
      DocumentsContract.Document.COLUMN_LAST_MODIFIED,
  };

  // ---- roots ------------------------------------------------------------------------------------

  /** The trees the user has granted, straight from the system. Persisted BY ANDROID, not by us — so
   *  there is no local list to drift out of step with what is actually permitted. */
  @PluginMethod
  public void list(PluginCall call) {
    JSArray out = new JSArray();
    for (android.content.UriPermission p : getContext().getContentResolver().getPersistedUriPermissions()) {
      if (!p.isReadPermission()) continue;
      JSObject o = new JSObject();
      o.put("id", p.getUri().toString());
      o.put("dir", prettyName(p.getUri()));
      out.put(o);
    }
    JSObject ret = new JSObject();
    ret.put("roots", out);
    call.resolve(ret);
  }

  @PluginMethod
  public void pick(PluginCall call) {
    Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT_TREE);
    i.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION
             | Intent.FLAG_GRANT_WRITE_URI_PERMISSION
             | Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION);
    startActivityForResult(call, i, "picked");
  }

  @ActivityCallback
  private void picked(PluginCall call, ActivityResult result) {
    if (call == null) return;
    if (result.getResultCode() != Activity.RESULT_OK || result.getData() == null
        || result.getData().getData() == null) {
      call.resolve(new JSObject());   // cancelled — not an error
      return;
    }
    Uri tree = result.getData().getData();
    // Persist it, or the grant dies with the process and the folder silently stops syncing after a
    // reboot with nothing to say why.
    try {
      getContext().getContentResolver().takePersistableUriPermission(tree,
          Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
    } catch (SecurityException e) {
      call.reject("could not keep access to that folder: " + e.getMessage());
      return;
    }
    JSObject o = new JSObject();
    o.put("id", tree.toString());
    o.put("dir", prettyName(tree));
    call.resolve(o);
  }

  @PluginMethod
  public void forget(PluginCall call) {
    String id = call.getString("id", "");
    try {
      getContext().getContentResolver().releasePersistableUriPermission(Uri.parse(id),
          Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
    } catch (Exception ignored) {}
    call.resolve();
  }

  // ---- scanning ---------------------------------------------------------------------------------

  @PluginMethod
  public void scan(PluginCall call) {
    final String id = call.getString("id", "");
    final boolean hash = Boolean.TRUE.equals(call.getBoolean("hash", false));
    final long maxBytes = call.getLong("maxBytes", 0L) == null ? 0L : call.getLong("maxBytes", 0L);
    final List<String> excludes = strings(call.getArray("excludes"));

    // Off the WebView thread: a Pictures folder is minutes of provider queries, and blocking here
    // freezes the UI the user is watching the progress in.
    getBridge().execute(() -> {
      try {
        Uri tree = Uri.parse(id);
        String rootDoc = DocumentsContract.getTreeDocumentId(tree);
        JSObject files = new JSObject();
        JSArray skipped = new JSArray();
        ContentResolver cr = getContext().getContentResolver();

        ArrayDeque<String[]> queue = new ArrayDeque<>();   // {documentId, relative path}
        queue.add(new String[]{ rootDoc, "" });
        while (!queue.isEmpty()) {
          String[] cur = queue.poll();
          Uri kids = DocumentsContract.buildChildDocumentsUriUsingTree(tree, cur[0]);
          Cursor c = null;
          try {
            c = cr.query(kids, COLS, null, null, null);
            if (c == null) { skipped.put(skip(cur[1], "unreadable")); continue; }
            while (c.moveToNext()) {
              String docId = c.getString(0), name = c.getString(1), mime = c.getString(2);
              long size = c.isNull(3) ? 0 : c.getLong(3);
              long mtime = c.isNull(4) ? 0 : c.getLong(4);
              if (name == null || isNoise(name)) continue;
              String rel = cur[1].isEmpty() ? name : cur[1] + "/" + name;
              if (Excludes.matches(rel, excludes)) continue;
              if (DocumentsContract.Document.MIME_TYPE_DIR.equals(mime)) {
                queue.add(new String[]{ docId, rel });
                continue;
              }
              if (maxBytes > 0 && size > maxBytes) { skipped.put(skip(rel, "too big", size)); continue; }
              JSObject e = new JSObject();
              e.put("size", size);
              e.put("mtime", mtime);
              if (hash) {
                String sha = sha256(cr, DocumentsContract.buildDocumentUriUsingTree(tree, docId));
                if (sha == null) { skipped.put(skip(rel, "unreadable")); continue; }
                // Stability: the provider's size/mtime after the read must match what it said
                // before. A photo still being written by the camera, or a file syncing in from
                // another app, hashes to bytes that were never a whole file — and a corrupt copy
                // with a valid checksum is worse than a delay.
                long[] now = statById(cr, tree, docId);
                if (now == null || now[0] != size || now[1] != mtime) {
                  skipped.put(skip(rel, "in use — will try again"));
                  continue;
                }
                e.put("sha", sha);
              }
              files.put(rel, e);
            }
          } catch (Exception ex) {
            skipped.put(skip(cur[1], "unreadable"));
          } finally { if (c != null) c.close(); }
        }
        JSObject ret = new JSObject();
        ret.put("files", files);
        ret.put("skipped", skipped);
        call.resolve(ret);
      } catch (Exception e) {
        call.reject("scan failed: " + e.getMessage());
      }
    });
  }

  // ---- reading / writing -------------------------------------------------------------------------

  @PluginMethod
  public void read(PluginCall call) {
    final String id = call.getString("id", ""), rel = call.getString("rel", "");
    getBridge().execute(() -> {
      try {
        Uri tree = Uri.parse(id);
        String docId = resolve(tree, rel, false);
        if (docId == null) { call.reject("not found: " + rel); return; }
        InputStream in = getContext().getContentResolver()
            .openInputStream(DocumentsContract.buildDocumentUriUsingTree(tree, docId));
        if (in == null) { call.reject("cannot open: " + rel); return; }
        java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
        byte[] buf = new byte[65536];
        int n;
        while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
        in.close();
        JSObject ret = new JSObject();
        ret.put("b64", Base64.encodeToString(bos.toByteArray(), Base64.NO_WRAP));
        call.resolve(ret);
      } catch (Exception e) { call.reject("read failed: " + e.getMessage()); }
    });
  }

  /**
   * Write, as close to atomically as SAF allows.
   *
   * There is no rename-over-an-existing-document: renameDocument fails if the name is taken. So the
   * bytes go to `name.pcpart` first, any existing document is moved into the trash rather than
   * deleted, and only then is the part renamed into place. The window where the target does not
   * exist is one rename wide, and what would have been lost in it is sitting in .pc-trash instead.
   */
  @PluginMethod
  public void write(PluginCall call) {
    final String id = call.getString("id", ""), rel = call.getString("rel", "");
    final String b64 = call.getString("b64", "");
    getBridge().execute(() -> {
      try {
        Uri tree = Uri.parse(id);
        byte[] bytes = Base64.decode(b64, Base64.DEFAULT);
        String name = baseName(rel), dirRel = dirName(rel);
        String dirId = resolve(tree, dirRel, true);
        if (dirId == null) { call.reject("cannot create " + dirRel); return; }
        ContentResolver cr = getContext().getContentResolver();

        String partId = childId(cr, tree, dirId, name + PART);
        if (partId != null) deleteDoc(cr, tree, partId);          // a previous crash's leftovers
        Uri partUri = DocumentsContract.createDocument(cr,
            DocumentsContract.buildDocumentUriUsingTree(tree, dirId), "application/octet-stream", name + PART);
        if (partUri == null) { call.reject("cannot write " + rel); return; }
        OutputStream out = cr.openOutputStream(partUri, "w");
        if (out == null) { call.reject("cannot open " + rel + " for writing"); return; }
        out.write(bytes);
        out.flush();
        out.close();

        String existing = childId(cr, tree, dirId, name);
        if (existing != null) trashDoc(cr, tree, existing, rel, call.getLong("when", 0L));
        DocumentsContract.renameDocument(cr, partUri, name);

        // The provider decides the mtime — SAF has no writable last-modified — so report what it
        // actually became. syncrun.js records THIS as the agreed state, which is what stops the next
        // sweep reading our own download as a local edit.
        String finalId = childId(cr, tree, dirId, name);
        long[] st = finalId == null ? null : statById(cr, tree, finalId);
        JSObject ret = new JSObject();
        ret.put("size", st != null ? st[0] : bytes.length);
        ret.put("mtime", st != null ? st[1] : System.currentTimeMillis());
        call.resolve(ret);
      } catch (Exception e) { call.reject("write failed: " + e.getMessage()); }
    });
  }

  @PluginMethod
  public void move(PluginCall call) {
    final String id = call.getString("id", ""), from = call.getString("from", ""), to = call.getString("to", "");
    getBridge().execute(() -> {
      try {
        Uri tree = Uri.parse(id);
        ContentResolver cr = getContext().getContentResolver();
        String srcId = resolve(tree, from, false);
        if (srcId == null) { call.reject("not found: " + from); return; }
        Uri src = DocumentsContract.buildDocumentUriUsingTree(tree, srcId);
        if (dirName(from).equals(dirName(to))) {
          DocumentsContract.renameDocument(cr, src, baseName(to));
        } else {
          String srcDir = resolve(tree, dirName(from), false), dstDir = resolve(tree, dirName(to), true);
          if (dstDir == null) { call.reject("cannot create " + dirName(to)); return; }
          DocumentsContract.moveDocument(cr, src,
              DocumentsContract.buildDocumentUriUsingTree(tree, srcDir),
              DocumentsContract.buildDocumentUriUsingTree(tree, dstDir));
          String moved = childId(cr, tree, dstDir, baseName(from));
          if (moved != null && !baseName(from).equals(baseName(to))) {
            DocumentsContract.renameDocument(cr,
                DocumentsContract.buildDocumentUriUsingTree(tree, moved), baseName(to));
          }
        }
        call.resolve();
      } catch (Exception e) { call.reject("move failed: " + e.getMessage()); }
    });
  }

  @PluginMethod
  public void trash(PluginCall call) {
    final String id = call.getString("id", ""), rel = call.getString("rel", "");
    final Long when = call.getLong("when", 0L);
    getBridge().execute(() -> {
      try {
        Uri tree = Uri.parse(id);
        ContentResolver cr = getContext().getContentResolver();
        String docId = resolve(tree, rel, false);
        if (docId == null) { call.reject("not found: " + rel); return; }
        String dest = trashDoc(cr, tree, docId, rel, when == null ? 0L : when);
        JSObject ret = new JSObject();
        ret.put("to", dest);
        call.resolve(ret);
      } catch (Exception e) { call.reject("delete failed: " + e.getMessage()); }
    });
  }

  @PluginMethod
  public void emptyTrash(PluginCall call) {
    final String id = call.getString("id", "");
    final int days = call.getInt("days", 30) == null ? 30 : call.getInt("days", 30);
    getBridge().execute(() -> {
      try {
        Uri tree = Uri.parse(id);
        ContentResolver cr = getContext().getContentResolver();
        String trashId = resolve(tree, TRASH, false);
        int removed = 0;
        if (trashId != null) {
          long cutoff = System.currentTimeMillis() - (long) days * 86400000L;
          Cursor c = cr.query(DocumentsContract.buildChildDocumentsUriUsingTree(tree, trashId),
                              COLS, null, null, null);
          if (c != null) {
            while (c.moveToNext()) {
              String docId = c.getString(0), name = c.getString(1);
              long when = Excludes.dayMillis(name);
              if (when <= 0 || when >= cutoff) continue;
              if (deleteDoc(cr, tree, docId)) removed++;
            }
            c.close();
          }
        }
        JSObject ret = new JSObject();
        ret.put("removed", removed);
        call.resolve(ret);
      } catch (Exception e) { call.reject("empty trash failed: " + e.getMessage()); }
    });
  }

  /** SAF has no tree change notification worth having, and polling one is the battery bug the sync
   *  policy exists to avoid. The answer is honest rather than a no-op that pretends. */
  @PluginMethod
  public void watch(PluginCall call) {
    JSObject o = new JSObject();
    o.put("watching", false);
    call.resolve(o);
  }

  @PluginMethod
  public void unwatch(PluginCall call) { call.resolve(); }

  /** What the battery policy reads (foldersync.js shouldSync). */
  @PluginMethod
  public void power(PluginCall call) {
    JSObject o = new JSObject();
    Context ctx = getContext();
    try {
      BatteryManager bm = (BatteryManager) ctx.getSystemService(Context.BATTERY_SERVICE);
      o.put("charging", bm != null && bm.isCharging());
      if (bm != null) o.put("battery", bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY));
    } catch (Exception e) { o.put("charging", false); }
    try {
      ConnectivityManager cm = (ConnectivityManager) ctx.getSystemService(Context.CONNECTIVITY_SERVICE);
      NetworkCapabilities nc = cm == null ? null : cm.getNetworkCapabilities(cm.getActiveNetwork());
      o.put("online", nc != null);
      // NOT_METERED is the capability that actually reflects the user's own "this is metered" flag
      // on a hotspot, which a Wi-Fi-vs-cellular check gets wrong every time.
      o.put("metered", nc != null && !nc.hasCapability(NetworkCapabilities.NET_CAPABILITY_NOT_METERED));
    } catch (Exception e) { o.put("online", true); o.put("metered", false); }
    call.resolve(o);
  }

  // ---- helpers ----------------------------------------------------------------------------------

  private static boolean isNoise(String name) {
    if (TRASH.equals(name) || name.endsWith(PART)) return true;
    return Excludes.isTempName(name);
  }

  private static JSObject skip(String path, String why) { return skip(path, why, -1); }
  private static JSObject skip(String path, String why, long size) {
    JSObject o = new JSObject();
    o.put("path", path);
    o.put("why", why);
    if (size >= 0) o.put("size", size);
    return o;
  }

  private static List<String> strings(JSArray a) {
    List<String> out = new ArrayList<>();
    if (a == null) return out;
    try { for (Object o : a.toList()) if (o != null) out.add(String.valueOf(o)); } catch (Exception ignored) {}
    return out;
  }

  private String prettyName(Uri tree) {
    String d = DocumentsContract.getTreeDocumentId(tree);
    int i = d == null ? -1 : d.lastIndexOf(':');
    String tail = i >= 0 ? d.substring(i + 1) : d;
    return (tail == null || tail.isEmpty()) ? tree.getLastPathSegment() : tail;
  }

  private static String baseName(String rel) {
    int i = rel.lastIndexOf('/');
    return i < 0 ? rel : rel.substring(i + 1);
  }
  private static String dirName(String rel) {
    int i = rel.lastIndexOf('/');
    return i < 0 ? "" : rel.substring(0, i);
  }

  private String childId(ContentResolver cr, Uri tree, String parentId, String name) {
    Cursor c = null;
    try {
      c = cr.query(DocumentsContract.buildChildDocumentsUriUsingTree(tree, parentId), COLS, null, null, null);
      if (c == null) return null;
      while (c.moveToNext()) if (name.equals(c.getString(1))) return c.getString(0);
    } catch (Exception ignored) {
    } finally { if (c != null) c.close(); }
    return null;
  }

  /** documentId for `rel`, creating the directories along the way when `create` is set. */
  private String resolve(Uri tree, String rel, boolean create) {
    ContentResolver cr = getContext().getContentResolver();
    String cur = DocumentsContract.getTreeDocumentId(tree);
    if (rel == null || rel.isEmpty()) return cur;
    for (String part : rel.split("/")) {
      if (part.isEmpty()) continue;
      String next = childId(cr, tree, cur, part);
      if (next == null) {
        if (!create) return null;
        // createDocument throws a CHECKED FileNotFoundException — the provider can be gone, the
        // volume unmounted, or the grant revoked between one segment and the next. Answering null is
        // right: every caller already treats "could not resolve" as a refusal, and a folder sync must
        // not take the app down because an SD card left the building mid-sweep.
        Uri made;
        try {
          made = DocumentsContract.createDocument(cr,
              DocumentsContract.buildDocumentUriUsingTree(tree, cur),
              DocumentsContract.Document.MIME_TYPE_DIR, part);
        } catch (Exception e) { return null; }
        if (made == null) return null;
        next = DocumentsContract.getDocumentId(made);
      }
      cur = next;
    }
    return cur;
  }

  private long[] statById(ContentResolver cr, Uri tree, String docId) {
    Cursor c = null;
    try {
      c = cr.query(DocumentsContract.buildDocumentUriUsingTree(tree, docId), COLS, null, null, null);
      if (c == null || !c.moveToFirst()) return null;
      return new long[]{ c.isNull(3) ? 0 : c.getLong(3), c.isNull(4) ? 0 : c.getLong(4) };
    } catch (Exception e) { return null;
    } finally { if (c != null) c.close(); }
  }

  private boolean deleteDoc(ContentResolver cr, Uri tree, String docId) {
    try { return DocumentsContract.deleteDocument(cr, DocumentsContract.buildDocumentUriUsingTree(tree, docId)); }
    catch (Exception e) { return false; }
  }

  /** Move a document into .pc-trash/<date>/, mirroring desktop/fsbridge.js — nothing is unlinked. */
  private String trashDoc(ContentResolver cr, Uri tree, String docId, String rel, long when) {
    String day = Excludes.dayName(when == 0 ? System.currentTimeMillis() : when);
    String destRel = TRASH + "/" + day + (dirName(rel).isEmpty() ? "" : "/" + dirName(rel));
    String destDir = resolve(tree, destRel, true);
    if (destDir == null) { deleteDoc(cr, tree, docId); return destRel + "/" + baseName(rel); }
    String name = baseName(rel);
    // Never overwrite what is already in the trash — a safety net that overwrites itself is not one.
    String want = name;
    for (int n = 2; n < 1000 && childId(cr, tree, destDir, want) != null; n++) {
      int dot = name.lastIndexOf('.');
      want = (dot > 0 ? name.substring(0, dot) : name) + " (" + n + ")" + (dot > 0 ? name.substring(dot) : "");
    }
    try {
      String srcDir = resolve(tree, dirName(rel), false);
      DocumentsContract.moveDocument(cr, DocumentsContract.buildDocumentUriUsingTree(tree, docId),
          DocumentsContract.buildDocumentUriUsingTree(tree, srcDir),
          DocumentsContract.buildDocumentUriUsingTree(tree, destDir));
      if (!want.equals(name)) {
        String moved = childId(cr, tree, destDir, name);
        if (moved != null) DocumentsContract.renameDocument(cr,
            DocumentsContract.buildDocumentUriUsingTree(tree, moved), want);
      }
    } catch (Exception e) {
      return null;
    }
    return destRel + "/" + want;
  }

  private String sha256(ContentResolver cr, Uri doc) {
    InputStream in = null;
    try {
      in = cr.openInputStream(doc);
      if (in == null) return null;
      MessageDigest md = MessageDigest.getInstance("SHA-256");
      byte[] buf = new byte[65536];
      int n;
      while ((n = in.read(buf)) > 0) md.update(buf, 0, n);
      StringBuilder sb = new StringBuilder();
      for (byte b : md.digest()) sb.append(Character.forDigit((b >> 4) & 0xf, 16))
                                   .append(Character.forDigit(b & 0xf, 16));
      return sb.toString();
    } catch (Exception e) { return null;
    } finally { try { if (in != null) in.close(); } catch (Exception ignored) {} }
  }
}
