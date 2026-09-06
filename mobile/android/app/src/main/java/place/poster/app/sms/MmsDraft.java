package place.poster.app.sms;

import android.content.Context;

import java.io.File;
import java.io.FileOutputStream;
import java.security.MessageDigest;

/** Durable foreground MMS draft. Bytes live in private app storage, never in an Activity field. */
final class MmsDraft {
    static final String READY = "ready", SENDING = "sending", SENT = "sent",
            FAILED = "failed", UNKNOWN = "delivery unknown";

    static final class Value {
        final String key, mime, name, state, error;
        final File file;
        Value(String k, String m, String n, String s, String e, File f) {
            key = k; mime = m; name = n; state = s; error = e; file = f;
        }
    }

    private MmsDraft() { }

    static String key(String address) {
        try {
            byte[] sum = MessageDigest.getInstance("SHA-256").digest(
                    (address == null ? "" : address).getBytes("UTF-8"));
            StringBuilder out = new StringBuilder();
            for (int i = 0; i < 12; i++) out.append(String.format("%02x", sum[i]));
            return out.toString();
        } catch (Throwable ignored) { return Integer.toHexString(String.valueOf(address).hashCode()); }
    }

    private static File dir(Context ctx) { return new File(ctx.getFilesDir(), "mms-drafts"); }
    private static File media(Context ctx, String key) { return new File(dir(ctx), key + ".media"); }
    private static android.content.SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences("poster_mms_drafts", Context.MODE_PRIVATE);
    }

    static Value save(Context ctx, String address, byte[] bytes, String mime, String name) throws Exception {
        return save(ctx, address, new java.io.ByteArrayInputStream(bytes), mime, name);
    }

    static Value save(Context ctx, String address, java.io.InputStream input, String mime, String name) throws Exception {
        String key = key(address); File d = dir(ctx);
        if (!d.exists() && !d.mkdirs()) throw new Exception("could not save picture draft");
        File tmp = new File(d, key + ".tmp");
        try (FileOutputStream out = new FileOutputStream(tmp)) {
            byte[] buffer = new byte[64 * 1024]; int n;
            while ((n = input.read(buffer)) != -1) out.write(buffer, 0, n);
            out.getFD().sync();
            if (tmp.length() == 0) throw new java.io.IOException("empty attachment");
        } catch (Exception e) { tmp.delete(); throw e; }
        File dst = media(ctx, key);
        if (!tmp.renameTo(dst)) { tmp.delete(); throw new Exception("could not save picture draft"); }
        prefs(ctx).edit().putString(key + ".mime", mime).putString(key + ".name", name)
                .putString(key + ".state", READY).remove(key + ".error").commit();
        return load(ctx, address);
    }

    static Value load(Context ctx, String address) {
        String key = key(address); File file = media(ctx, key);
        if (!file.isFile() || file.length() == 0) return null;
        android.content.SharedPreferences p = prefs(ctx);
        return new Value(key, p.getString(key + ".mime", "image/jpeg"),
                p.getString(key + ".name", "attachment"), p.getString(key + ".state", READY),
                p.getString(key + ".error", ""), file);
    }

    static void state(Context ctx, String key, String state, String error) {
        prefs(ctx).edit().putString(key + ".state", state)
                .putString(key + ".error", error == null ? "" : error).commit();
    }

    static void remove(Context ctx, String address) {
        String key = key(address); media(ctx, key).delete();
        prefs(ctx).edit().remove(key + ".mime").remove(key + ".name")
                .remove(key + ".state").remove(key + ".error").commit();
    }
}
