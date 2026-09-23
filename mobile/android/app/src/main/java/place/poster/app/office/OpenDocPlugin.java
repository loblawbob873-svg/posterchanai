package place.poster.app.office;

import android.content.Intent;
import android.content.pm.PackageManager;
import android.database.Cursor;
import android.net.Uri;
import android.provider.OpenableColumns;
import android.util.Base64;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * THE DOCUMENT ANDROID HANDED US -- "Open with PosterChan Office", a share to that entry, or an EDIT.
 *
 * `pending` answers the current launch intent once per arrival (MainActivity counts them in
 * {@link #nonce}, the same way it counts shares), with the bytes, a name and whether the source may
 * be written. `save` writes the edited bytes BACK TO THAT SAME URI -- only that one, only for the
 * arrival it came with, and only when Android granted write access. A read-only source is not an
 * error to hide: the client says so and saves a copy instead.
 *
 * Work happens on its own thread with its own try/catch: a Runnable given to the bridge executor
 * runs outside Capacitor's guard, and a throw there kills the process with nothing in the page.
 */
@CapacitorPlugin(name = "OpenDoc")
public final class OpenDocPlugin extends Plugin {
    /** Bumped by MainActivity for every document arrival (cold start and onNewIntent). */
    public static volatile int nonce = 0;

    public static final String OFFICE_ALIAS = "place.poster.app.OpenInOffice";
    private static final int MAX = 32 * 1024 * 1024;

    /** Whether an intent is a document arrival -- MainActivity asks this before bumping {@link #nonce}. */
    public static boolean isDocIntent(Intent i) {
        if (i == null) return false;
        Uri u = sourceOf(i);
        String name = u == null ? "" : u.getLastPathSegment();
        return u != null && DocIntent.isOpen(i.getAction(), viaOffice(i), i.getType(), name);
    }

    static boolean viaOffice(Intent i) {
        return i.getComponent() != null && OFFICE_ALIAS.equals(i.getComponent().getClassName());
    }

    static Uri sourceOf(Intent i) {
        if (Intent.ACTION_SEND.equals(i.getAction())) {
            Object o = i.getParcelableExtra(Intent.EXTRA_STREAM);
            return o instanceof Uri ? (Uri) o : null;
        }
        return i.getData();
    }

    @PluginMethod
    public void pending(final PluginCall call) {
        final Intent i = getActivity() == null ? null : getActivity().getIntent();
        final int n = nonce;
        new Thread(() -> {
            try {
                JSObject out = new JSObject();
                out.put("nonce", n);
                if (i == null || n == 0) { call.resolve(out); return; }
                Uri uri = sourceOf(i);
                if (uri == null) { call.resolve(out); return; }
                String display = displayName(uri);
                String mime = i.getType();
                if (mime == null || mime.isEmpty()) mime = getContext().getContentResolver().getType(uri);
                String kind = DocIntent.kindOf(mime, display == null || display.isEmpty() ? uri.getLastPathSegment() : display);
                if (!DocIntent.isOpen(i.getAction(), viaOffice(i), mime, display == null ? uri.getLastPathSegment() : display)) {
                    call.resolve(out); return;
                }
                byte[] bytes = read(uri);
                out.put("kind", kind);
                out.put("name", DocIntent.nameFor(display, uri.getLastPathSegment(), kind));
                out.put("mime", mime == null ? "" : mime);
                out.put("size", bytes.length);
                out.put("writable", writable(i, uri));
                out.put("data", Base64.encodeToString(bytes, Base64.NO_WRAP));
                call.resolve(out);
            } catch (Throwable e) {
                call.reject(e.getMessage() == null ? "could not read the document" : e.getMessage());
            }
        }, "pc-opendoc-read").start();
    }

    @PluginMethod
    public void save(final PluginCall call) {
        final Intent i = getActivity() == null ? null : getActivity().getIntent();
        final int want = call.getInt("nonce", -1);
        final String data = call.getString("data", "");
        new Thread(() -> {
            try {
                /* ONLY THE ARRIVAL THE EDIT CAME FROM. A second document opened since then has
                 * replaced the intent; writing this document's bytes over THAT one would be the
                 * worst possible bug here, so a stale nonce is refused outright. */
                if (i == null || want != nonce) throw new IllegalStateException("a different document has been opened since");
                Uri uri = sourceOf(i);
                if (uri == null || !writable(i, uri)) throw new SecurityException("read-only");
                if (data.length() > ((MAX + 2) / 3) * 4) throw new IllegalArgumentException("document is too large");
                byte[] bytes = Base64.decode(data, Base64.DEFAULT);
                if (bytes.length == 0) throw new IllegalArgumentException("empty document");
                try (OutputStream os = getContext().getContentResolver().openOutputStream(uri, "wt")) {
                    if (os == null) throw new java.io.IOException("the provider gave no stream");
                    os.write(bytes);
                }
                JSObject out = new JSObject();
                out.put("ok", true);
                out.put("size", bytes.length);
                call.resolve(out);
            } catch (Throwable e) {
                call.reject(e instanceof SecurityException ? "read-only" : (e.getMessage() == null ? "could not save" : e.getMessage()));
            }
        }, "pc-opendoc-save").start();
    }

    private boolean writable(Intent i, Uri uri) {
        if ((i.getFlags() & Intent.FLAG_GRANT_WRITE_URI_PERMISSION) != 0) return true;
        try {
            return getContext().checkCallingOrSelfUriPermission(uri, Intent.FLAG_GRANT_WRITE_URI_PERMISSION)
                    == PackageManager.PERMISSION_GRANTED;
        } catch (Throwable t) { return false; }
    }

    private String displayName(Uri uri) {
        if (!"content".equals(uri.getScheme())) return uri.getLastPathSegment();
        try (Cursor c = getContext().getContentResolver().query(uri, new String[]{OpenableColumns.DISPLAY_NAME}, null, null, null)) {
            if (c != null && c.moveToFirst()) return c.getString(0);
        } catch (Throwable ignored) { }
        return null;
    }

    private byte[] read(Uri uri) throws java.io.IOException {
        try (InputStream in = getContext().getContentResolver().openInputStream(uri)) {
            if (in == null) throw new java.io.IOException("the provider gave no stream");
            ByteArrayOutputStream buf = new ByteArrayOutputStream();
            byte[] chunk = new byte[64 * 1024];
            int r;
            while ((r = in.read(chunk)) > 0) {
                if (buf.size() + r > MAX) throw new java.io.IOException("document is larger than 32 MB");
                buf.write(chunk, 0, r);
            }
            if (buf.size() == 0) throw new java.io.IOException("the document is empty");
            return buf.toByteArray();
        }
    }
}
