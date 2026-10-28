package place.poster.app.files;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.LinkedHashMap;

/**
 * THE PHONE'S OWN STORAGE, for Files' "This phone" -- the Android half of the desktop's hostfs.js,
 * with the same answers in the same shapes, so hostfiles.js draws both without knowing which it has.
 * Asked for 2026-10-28: "android: no way to browse user's android storage on files".
 *
 * EVERY PATH IS CONFINED TO A STORAGE ROOT (internal shared storage, an SD card, a USB stick).
 * "All files access" lets this process read far more than a person's files -- other apps' folders
 * under Android/data on older releases, /system, /proc -- and none of that is what a file manager
 * for somebody's photos and documents should hand to a web page. A path is resolved CANONICALLY
 * (symlinks followed) before the check, so `Download/link-to-/data` cannot step outside either.
 *
 * Pure Java, no Android imports: tests/test_android_phone_files.py compiles it and RUNS it against
 * a real temporary directory.
 */
public final class PhoneFilesCore {

    /** The largest file handed across the bridge whole (base64 in a JSON string). */
    public static final long READ_MAX = 64L * 1024 * 1024;
    public static final long WRITE_MAX = 64L * 1024 * 1024;
    public static final String TRASH = ".Trash-PosterChan";

    private final List<File> roots;

    public PhoneFilesCore(List<File> roots) {
        List<File> out = new ArrayList<>();
        for (File r : roots) {
            try { if (r != null && r.isDirectory()) out.add(r.getCanonicalFile()); } catch (IOException ignored) { }
        }
        this.roots = out;
    }

    public List<File> roots() { return roots; }

    /** The root holding `f`, or null. `f` must already be canonical. */
    File rootOf(File f) {
        String p = f.getPath();
        for (File r : roots) {
            String rp = r.getPath();
            if (p.equals(rp) || p.startsWith(rp.endsWith("/") ? rp : rp + "/")) return r;
        }
        return null;
    }

    /** The canonical file for `path`, refused unless it lies inside a storage root. */
    public File inside(String path) throws IOException {
        if (path == null || path.isEmpty() || path.indexOf('\0') >= 0) throw new IOException("not a path");
        if (!path.startsWith("/")) throw new IOException("not a path");
        File f = new File(path).getCanonicalFile();
        if (rootOf(f) == null) throw new IOException("outside this phone's storage");
        return f;
    }

    /** A NAME, NOT A PATH: `../../x` in a "new folder" box would be a folder created somewhere nobody asked. */
    public static String leaf(String name) throws IOException {
        String n = name == null ? "" : name.trim();
        if (n.isEmpty() || n.equals(".") || n.equals("..") || n.indexOf('/') >= 0 || n.indexOf('\0') >= 0)
            throw new IOException("that is not a name");
        return n;
    }

    public Map<String, Object> shape(File f) {
        Map<String, Object> e = new LinkedHashMap<>();
        boolean dir = f.isDirectory();
        e.put("name", f.getName());
        e.put("path", f.getPath());
        e.put("dir", dir);
        e.put("size", dir ? 0L : f.length());
        e.put("mtime", f.lastModified());
        e.put("created", 0L);
        e.put("link", false);
        e.put("hidden", f.getName().startsWith("."));
        e.put("broken", false);
        return e;
    }

    /** What is in `dir`. Throws when it cannot be read -- "I could not ask" is never "it is empty". */
    public Map<String, Object> list(String dir) throws IOException {
        File d = inside(dir);
        if (!d.isDirectory()) throw new IOException("not a folder");
        File[] kids = d.listFiles();
        if (kids == null) throw new IOException("this folder cannot be read (is file access allowed?)");
        List<Map<String, Object>> entries = new ArrayList<>();
        for (File k : kids) {
            if (k.getName().equals(TRASH)) continue;          // our own bin is not a folder anybody made
            entries.add(shape(k));
        }
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("path", d.getPath());
        File root = rootOf(d), up = d.getParentFile();
        out.put("parent", (root == null || d.equals(root) || up == null) ? null : up.getPath());
        out.put("entries", entries);
        return out;
    }

    public String mkdir(String dir, String name) throws IOException {
        File d = inside(dir);
        File p = new File(d, leaf(name));
        if (p.exists()) throw new IOException("there is already something called that");
        if (!p.mkdir()) throw new IOException("could not make the folder");
        return p.getPath();
    }

    /** Rename in place. Never overwrites: on Android as on POSIX that is somebody's other file gone. */
    public String rename(String from, String to) throws IOException {
        File f = inside(from);
        if (roots.contains(f)) throw new IOException("a storage root cannot be renamed");
        File p = new File(f.getParentFile(), leaf(to));
        if (p.exists()) throw new IOException("there is already something called that");
        if (!f.renameTo(p)) throw new IOException("could not rename it");
        return p.getPath();
    }

    /** Into this volume's own bin (a rename, so nothing is copied and nothing is lost). Returns where it went. */
    public String trash(String target) throws IOException {
        File t = inside(target);
        File root = rootOf(t);
        if (t.equals(root)) throw new IOException("refusing to delete a storage root");
        if (!t.exists()) throw new IOException("it is not there");
        File bin = new File(root, TRASH);
        if (!bin.isDirectory() && !bin.mkdirs()) throw new IOException("could not make the bin");
        File to = new File(bin, freeName(bin, t.getName()));
        if (!t.renameTo(to)) throw new IOException("could not move it to the bin");
        return to.getPath();
    }

    static String freeName(File dir, String name) throws IOException {
        if (!new File(dir, name).exists()) return name;
        int dot = name.lastIndexOf('.');
        String stem = dot > 0 ? name.substring(0, dot) : name, ext = dot > 0 ? name.substring(dot) : "";
        for (int i = 2; i < 10000; i++) {
            String n = stem + "." + i + ext;
            if (!new File(dir, n).exists()) return n;
        }
        throw new IOException("nowhere to put it");
    }

    /** "report (copy).pdf", then "report (copy 2).pdf" -- a copy pasted into its own folder. */
    static String copyName(File dir, String name) throws IOException {
        int dot = name.lastIndexOf('.');
        String stem = dot > 0 ? name.substring(0, dot) : name, ext = dot > 0 ? name.substring(dot) : "";
        for (int i = 1; i < 10000; i++) {
            String n = stem + (i == 1 ? " (copy)" : " (copy " + i + ")") + ext;
            if (!new File(dir, n).exists()) return n;
        }
        throw new IOException("nowhere to put it");
    }

    /** Copy or move into a folder. Collisions are refused, never replaced; a folder never goes inside itself. */
    public List<String[]> transfer(List<String> items, String destination, boolean move) throws IOException {
        File dest = inside(destination);
        if (!dest.isDirectory()) throw new IOException("destination is not a folder");
        LinkedHashSet<File> sources = new LinkedHashSet<>();
        for (String s : items) sources.add(inside(s));
        if (sources.isEmpty()) throw new IOException("nothing selected");
        List<File[]> plan = new ArrayList<>();
        for (File from : sources) {
            if (!from.exists()) throw new IOException(from.getName() + " is not there");
            if (roots.contains(from)) throw new IOException("a storage root cannot be moved");
            boolean here = !move && dest.equals(from.getParentFile());
            File to = new File(dest, here ? copyName(dest, from.getName()) : from.getName());
            if (to.equals(from)) throw new IOException("source and destination are the same");
            String fp = from.getPath();
            if (dest.equals(from) || dest.getPath().startsWith(fp + "/"))
                throw new IOException("a folder cannot be copied inside itself");
            if (to.exists()) throw new IOException("there is already something called " + to.getName());
            plan.add(new File[]{from, to});
        }
        List<String[]> done = new ArrayList<>();
        for (File[] x : plan) {
            if (move && x[0].renameTo(x[1])) { done.add(new String[]{x[0].getPath(), x[1].getPath()}); continue; }
            copyTree(x[0], x[1]);                     // a copy, or a move across volumes (rename refuses those)
            if (move) deleteTree(x[0]);
            done.add(new String[]{x[0].getPath(), x[1].getPath()});
        }
        return done;
    }

    static void copyTree(File from, File to) throws IOException {
        if (from.isDirectory()) {
            if (!to.mkdir()) throw new IOException("could not make " + to.getName());
            File[] kids = from.listFiles();
            if (kids == null) throw new IOException("could not read " + from.getName());
            for (File k : kids) copyTree(k, new File(to, k.getName()));
            return;
        }
        try (InputStream in = new FileInputStream(from); OutputStream out = new FileOutputStream(to, false)) {
            byte[] buf = new byte[1 << 16];
            for (int n; (n = in.read(buf)) > 0; ) out.write(buf, 0, n);
        }
        to.setLastModified(from.lastModified());
    }

    static void deleteTree(File f) throws IOException {
        if (f.isDirectory()) {
            File[] kids = f.listFiles();
            if (kids != null) for (File k : kids) deleteTree(k);
        }
        if (f.exists() && !f.delete()) throw new IOException("could not remove " + f.getName());
    }

    /** The whole file, bounded BEFORE the read: it crosses the bridge as base64 into the WebView's heap. */
    public byte[] read(String path, long max) throws IOException {
        File f = inside(path);
        if (f.isDirectory()) throw new IOException("that is a folder");
        long cap = Math.min(max > 0 ? max : READ_MAX, READ_MAX);
        if (f.length() > cap) throw new IOException("that file is too big to open here");
        byte[] out = new byte[(int) f.length()];
        try (InputStream in = new FileInputStream(f)) {
            int off = 0;
            while (off < out.length) { int n = in.read(out, off, out.length - off); if (n < 0) break; off += n; }
            if (off != out.length) throw new IOException("the file changed while it was read");
        }
        return out;
    }

    /**
     * Replace an EXISTING file atomically (temp + rename), with the desktop's compare-and-swap: a file that
     * changed on disk since it was opened is refused ("changed-on-disk") instead of overwritten.
     */
    public Map<String, Object> writeBytes(String path, byte[] data, long mtime) throws IOException {
        File f = inside(path);
        if (!f.isFile()) throw new IOException(f.isDirectory() ? "that is a folder" : "it is not there");
        if (mtime > 0 && Math.abs(f.lastModified() - mtime) > 1000) throw new IOException("changed-on-disk");
        if (data.length > WRITE_MAX) throw new IOException("that file is too big to save here");
        File tmp = new File(f.getParentFile(), ".pc-" + f.getName() + ".tmp");
        try (OutputStream out = new FileOutputStream(tmp, false)) { out.write(data); }
        if (!tmp.renameTo(f)) { tmp.delete(); throw new IOException("could not save it"); }
        Map<String, Object> r = new LinkedHashMap<>();
        r.put("path", f.getPath()); r.put("size", f.length()); r.put("mtime", f.lastModified());
        return r;
    }

    /** A MIME type from the name, for "open with". */
    public static String mimeOf(String name) {
        String n = name == null ? "" : name.toLowerCase(Locale.ROOT);
        int dot = n.lastIndexOf('.');
        String ext = dot >= 0 ? n.substring(dot + 1) : "";
        switch (ext) {
            case "jpg": case "jpeg": return "image/jpeg";
            case "png": return "image/png";
            case "gif": return "image/gif";
            case "webp": return "image/webp";
            case "heic": return "image/heic";
            case "mp4": case "m4v": return "video/mp4";
            case "webm": return "video/webm";
            case "mkv": return "video/x-matroska";
            case "mp3": return "audio/mpeg";
            case "m4a": return "audio/mp4";
            case "ogg": case "opus": return "audio/ogg";
            case "flac": return "audio/flac";
            case "wav": return "audio/wav";
            case "pdf": return "application/pdf";
            case "txt": case "md": case "log": return "text/plain";
            case "html": case "htm": return "text/html";
            case "zip": return "application/zip";
            case "apk": return "application/vnd.android.package-archive";
            case "doc": return "application/msword";
            case "docx": return "application/vnd.openxmlformats-officedocument.wordprocessingml.document";
            case "xlsx": return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
            case "pptx": return "application/vnd.openxmlformats-officedocument.presentationml.presentation";
            case "odt": return "application/vnd.oasis.opendocument.text";
            default: return "*/*";
        }
    }
}
