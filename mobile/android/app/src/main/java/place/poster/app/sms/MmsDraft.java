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

    private static final java.util.concurrent.ConcurrentHashMap<String, Slot> copies =
            new java.util.concurrent.ConcurrentHashMap<>();
    private static final class Slot { long generation; }
    static final class Copy {
        final String key; final Slot slot; final long generation;
        Copy(String key, Slot slot, long generation) {
            this.key = key; this.slot = slot; this.generation = generation;
        }
    }
    private static Slot slot(String key) { return copies.computeIfAbsent(key, ignored -> new Slot()); }
    static Copy beginCopy(String address) {
        String key = key(address); Slot slot = slot(key);
        synchronized (slot) { return new Copy(key, slot, ++slot.generation); }
    }
    /** Observe ownership for a confirmation dialog without invalidating an active copy. */
    static Copy snapshot(String address) {
        String key = key(address); Slot slot = slot(key);
        synchronized (slot) { return new Copy(key, slot, slot.generation); }
    }
    static boolean isCurrent(Copy copy) {
        synchronized (copy.slot) { return copy.slot.generation == copy.generation; }
    }

    static String key(String address) {
        try {
            byte[] sum = MessageDigest.getInstance("SHA-256").digest(
                    (address == null ? "" : address).getBytes("UTF-8"));
            StringBuilder out = new StringBuilder();
            for (int i = 0; i < 12; i++) out.append(String.format("%02x", sum[i]));
            return out.toString();
        } catch (Throwable ignored) { return Integer.toHexString(String.valueOf(address).hashCode()); }
    }

    /* THE TYPED MESSAGE IS A DRAFT TOO, and it is filed the same way the picture is: under the
     * conversation, never in an Activity field.
     *
     * ThreadActivity is `singleTop`, and the SMS notification opens it with CLEAR_TOP — so tapping
     * the card for an incoming text while the screen is already open does NOT build a new screen,
     * it calls onNewIntent. That changed `address` and reloaded the list while leaving the compose
     * box exactly as it was, so a half-typed reply to one person was sitting in somebody else's
     * conversation, and send() -- which reads the CURRENT address -- would have sent it to them.
     * Reported as the text input field containing the message of a different, incoming message.
     *
     * Stored beside the attachment draft rather than held in a field, because the two events that
     * take this screen away (a notification hand-over, and Android destroying a backgrounded
     * activity) are the two that must not lose it. */
    static String text(Context ctx, String address) {
        if (address == null || address.isEmpty()) return "";
        return prefs(ctx).getString(key(address) + ".text", "");
    }

    static void setText(Context ctx, String address, String body) {
        if (address == null || address.isEmpty()) return;
        String k = key(address) + ".text";
        if (body == null || body.isEmpty()) prefs(ctx).edit().remove(k).apply();
        else prefs(ctx).edit().putString(k, body).apply();
    }

    private static File dir(Context ctx) { return new File(ctx.getFilesDir(), "mms-drafts"); }
    private static File media(Context ctx, String key) { return new File(dir(ctx), key + ".media"); }
    private static boolean staleTempsCleaned;
    private static synchronized File temporary(File directory, String key) throws java.io.IOException {
        // All MMS components share the app process. Before its first copy exists, every .tmp
        // belongs to a previous process that died before finally could delete it. Never sweep
        // again: another recipient may have a provider read in progress outside this lock.
        if (!staleTempsCleaned) {
            File[] stale = directory.listFiles(file -> file.isFile() && file.getName().endsWith(".tmp"));
            if (stale != null) for (File file : stale) file.delete();
            staleTempsCleaned = true;
        }
        return File.createTempFile(key + "-", ".tmp", directory);
    }
    private static android.content.SharedPreferences prefs(Context ctx) {
        return ctx.getSharedPreferences("poster_mms_drafts", Context.MODE_PRIVATE);
    }

    static Value save(Context ctx, String address, byte[] bytes, String mime, String name) throws Exception {
        return save(ctx, address, new java.io.ByteArrayInputStream(bytes), mime, name);
    }

    static Value save(Context ctx, String address, java.io.InputStream input, String mime, String name) throws Exception {
        return save(ctx, address, input, mime, name, beginCopy(address));
    }

    static Value save(Context ctx, String address, java.io.InputStream input, String mime,
                      String name, Copy copy) throws Exception {
        String key = key(address); File d = dir(ctx);
        if (!key.equals(copy.key)) throw new IllegalArgumentException("draft copy recipient changed");
        if (!d.isDirectory() && !d.mkdirs() && !d.isDirectory()) throw new Exception("could not save picture draft");
        File tmp = temporary(d, key);
        try (FileOutputStream out = new FileOutputStream(tmp)) {
            byte[] buffer = new byte[64 * 1024]; int n;
            while ((n = input.read(buffer)) != -1) out.write(buffer, 0, n);
            out.getFD().sync();
            if (tmp.length() == 0) throw new java.io.IOException("empty attachment");
        } catch (Exception e) { tmp.delete(); throw e; }
        // Slow provider reads happen above, outside the recipient lock. Only the latest
        // accepted copy may replace the durable bytes and metadata, including across Activities.
        synchronized (copy.slot) {
            if (!isCurrent(copy)) { tmp.delete(); throw new java.io.IOException("attachment copy superseded"); }
            File dst = media(ctx, key);
            if (!tmp.renameTo(dst)) { tmp.delete(); throw new Exception("could not save picture draft"); }
            prefs(ctx).edit().putString(key + ".mime", mime).putString(key + ".name", name)
                    .putString(key + ".state", READY).remove(key + ".error").commit();
            return load(ctx, address);
        }
    }

    static Value load(Context ctx, String address) {
        String key = key(address); File file = media(ctx, key);
        synchronized (slot(key)) {
            if (!file.isFile() || file.length() == 0) return null;
            android.content.SharedPreferences p = prefs(ctx);
            return new Value(key, p.getString(key + ".mime", "image/jpeg"),
                    p.getString(key + ".name", "attachment"), p.getString(key + ".state", READY),
                    p.getString(key + ".error", ""), file);
        }
    }

    static void state(Context ctx, String key, String state, String error) {
        Slot slot = slot(key);
        synchronized (slot) {
            ++slot.generation;
            prefs(ctx).edit().putString(key + ".state", state)
                    .putString(key + ".error", error == null ? "" : error).commit();
        }
    }

    /* THE CARRIER'S VERDICT ONLY LANDS ON THE DRAFT THAT IS STILL WAITING FOR IT.
     *
     * A picture handed to the carrier leaves the composer at once (ThreadActivity.sendMms), so by the
     * time MmsSendReceiver hears back, this conversation's draft is either gone or is a NEW picture
     * the person has since picked. Writing SENT/FAILED over that one would make send() refuse it
     * ("sent") or offer a retry for something never sent. The result is recorded on the message row
     * (provider box + MmsFailures), which is where a failed send is retried from. */
    static void result(Context ctx, String key, String state, String error) {
        Slot slot = slot(key);
        synchronized (slot) {
            if (!media(ctx, key).isFile() || !SENDING.equals(prefs(ctx).getString(key + ".state", ""))) return;
            ++slot.generation;
            prefs(ctx).edit().putString(key + ".state", state)
                    .putString(key + ".error", error == null ? "" : error).commit();
        }
    }

    static void remove(Context ctx, String address) {
        String key = key(address); Slot slot = slot(key);
        synchronized (slot) {
            ++slot.generation;
            media(ctx, key).delete();
            prefs(ctx).edit().remove(key + ".mime").remove(key + ".name")
                    .remove(key + ".state").remove(key + ".error").commit();
        }
    }
}
