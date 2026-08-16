package place.poster.app.sync;

import android.content.ContentResolver;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.provider.DocumentsContract;

import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.security.MessageDigest;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * One granted folder, as a filesystem — the SAF half of folder sync, lifted out of
 * {@link FolderSyncPlugin} so it has exactly one implementation.
 *
 * IT MOVED BECAUSE A BACKGROUND SWEEP HAS NO WEBVIEW. Every operation here used to be a
 * {@code @PluginMethod}: reachable only by a page calling across the Capacitor bridge, which is
 * precisely the half Android takes away when the screen goes off. The plugin still exposes all of
 * them and now delegates, so the foreground path is unchanged byte for byte; {@link NativeSweep}
 * calls the same methods directly.
 *
 * THE RULES THAT LIVE IN THIS FILE ARE THE ONES THAT CAN DESTROY A FILE, and each is here because it
 * did:
 *
 *   * nothing is deleted in place. A local delete is a MOVE into `.pc-trash/<date>/`, and when the
 *     trash cannot be created the delete is REFUSED rather than turned into an unlink — the failure
 *     is temporary, the deletion would not be.
 *   * there is no rename-over-an-existing-document in SAF, so a write goes to `name.pcpart`, the old
 *     document is trashed, and only then is the part renamed into place. Every step is CHECKED: an
 *     unchecked commit reported a successful download while the old file was still there.
 *   * `isEmptyDir` fails CLOSED, because deleteDocument on a directory is recursive here — the
 *     emptiness check IS the safety, where a desktop gets it from the syscall.
 *   * a scan re-stats after hashing: a file still being written hashes to bytes that were never a
 *     whole file, and a corrupt copy with a valid checksum is worse than a delay.
 */
public final class SafFs {

    public static final String PART = ".pcpart";
    public static final String TRASH = ".pc-trash";

    static final String[] COLS = {
            DocumentsContract.Document.COLUMN_DOCUMENT_ID,
            DocumentsContract.Document.COLUMN_DISPLAY_NAME,
            DocumentsContract.Document.COLUMN_MIME_TYPE,
            DocumentsContract.Document.COLUMN_SIZE,
            DocumentsContract.Document.COLUMN_LAST_MODIFIED,
    };

    private final ContentResolver cr;
    private final Uri tree;

    public SafFs(Context ctx, String treeUri) {
        this.cr = ctx.getContentResolver();
        this.tree = Uri.parse(treeUri);
    }

    public Uri tree() { return tree; }

    public static boolean isNoise(String name) {
        if (TRASH.equals(name) || name.endsWith(PART)) return true;
        return Excludes.isTempName(name);
    }

    public static String baseName(String rel) {
        int i = rel.lastIndexOf('/');
        return i < 0 ? rel : rel.substring(i + 1);
    }

    public static String dirName(String rel) {
        int i = rel.lastIndexOf('/');
        return i < 0 ? "" : rel.substring(0, i);
    }

    // ------------------------------------------------------------------------------ lookups

    public String childId(String parentId, String name) {
        Cursor c = null;
        try {
            c = cr.query(DocumentsContract.buildChildDocumentsUriUsingTree(tree, parentId), COLS,
                         null, null, null);
            if (c == null) return null;
            while (c.moveToNext()) if (name.equals(c.getString(1))) return c.getString(0);
        } catch (Exception ignored) {
        } finally { if (c != null) c.close(); }
        return null;
    }

    /** documentId for `rel`, creating the directories along the way when `create` is set. */
    public String resolve(String rel, boolean create) {
        String cur = DocumentsContract.getTreeDocumentId(tree);
        if (rel == null || rel.isEmpty()) return cur;
        for (String part : rel.split("/")) {
            if (part.isEmpty()) continue;
            String next = childId(cur, part);
            if (next == null) {
                if (!create) return null;
                // createDocument throws a CHECKED FileNotFoundException — the provider can be gone,
                // the volume unmounted, or the grant revoked between one segment and the next.
                // Answering null is right: every caller already treats "could not resolve" as a
                // refusal, and a folder sync must not take the app down because an SD card left the
                // building mid-sweep.
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

    /** {size, mtime} for a document id, or null. */
    public long[] statById(String docId) {
        Cursor c = null;
        try {
            c = cr.query(DocumentsContract.buildDocumentUriUsingTree(tree, docId), COLS, null, null, null);
            if (c == null || !c.moveToFirst()) return null;
            return new long[]{ c.isNull(3) ? 0 : c.getLong(3), c.isNull(4) ? 0 : c.getLong(4) };
        } catch (Exception e) { return null;
        } finally { if (c != null) c.close(); }
    }

    public boolean deleteDoc(String docId) {
        try {
            return DocumentsContract.deleteDocument(cr,
                    DocumentsContract.buildDocumentUriUsingTree(tree, docId));
        } catch (Exception e) { return false; }
    }

    /**
     * Has this directory nothing in it?
     *
     * FAILS CLOSED, and here that is not a style choice. `deleteDocument` on a directory deletes it
     * RECURSIVELY — there is no SAF equivalent of rmdir refusing a non-empty one — so on this
     * platform the emptiness check IS the safety. A query that returns null or throws (provider gone,
     * volume unmounted, grant revoked mid-sweep) must answer "not empty", because the cost of being
     * wrong in the other direction is somebody's folder.
     */
    public boolean isEmptyDir(String docId) {
        Cursor c = null;
        try {
            c = cr.query(DocumentsContract.buildChildDocumentsUriUsingTree(tree, docId),
                         new String[]{ DocumentsContract.Document.COLUMN_DOCUMENT_ID }, null, null, null);
            if (c == null) return false;
            return !c.moveToFirst();
        } catch (Exception e) {
            return false;
        } finally { if (c != null) try { c.close(); } catch (Exception ignored) { } }
    }

    /**
     * The directories a delete leaves behind. A manifest holds PATHS, never directories, so deleting
     * a folder tombstones every file in it and leaves the empty tree standing exactly where the user
     * deleted it. Walks UP, stops at the first directory that still holds something, at the tree
     * root, and inside `.pc-trash`.
     */
    public void pruneEmptyDirs(String rel) {
        if (rel == null) return;
        String[] parts = rel.split("/");
        if (parts.length == 0 || TRASH.equals(parts[0])) return;
        String root = DocumentsContract.getTreeDocumentId(tree);
        for (int depth = parts.length - 1; depth >= 1; depth--) {
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < depth; i++) { if (sb.length() > 0) sb.append('/'); sb.append(parts[i]); }
            String docId = resolve(sb.toString(), false);
            if (docId == null || docId.equals(root)) return;
            if (!isEmptyDir(docId)) return;
            if (!deleteDoc(docId)) return;
        }
    }

    /** Move a document into .pc-trash/<date>/, mirroring desktop/fsbridge.js — nothing is unlinked. */
    public String trashDoc(String docId, String rel, long when) {
        String day = Excludes.dayName(when == 0 ? System.currentTimeMillis() : when);
        String destRel = TRASH + "/" + day + (dirName(rel).isEmpty() ? "" : "/" + dirName(rel));
        String destDir = resolve(destRel, true);
        /* REFUSE. Do not delete.
         *
         * This used to unlink the user's file when the trash directory could not be created, and
         * return a path implying it had been trashed — so the caller recorded a successful delete for
         * a file that no longer exists anywhere. It breaks the one guarantee this feature makes, and
         * it fires exactly when things are already wrong: a partially revoked grant, an unmounted
         * volume, a FILE named .pc-trash shadowing the directory. Every one of those is temporary;
         * the deletion is not. */
        if (destDir == null) return null;
        String name = baseName(rel);
        // Never overwrite what is already in the trash — a safety net that overwrites itself is none.
        String want = name;
        for (int n = 2; n < 1000 && childId(destDir, want) != null; n++) {
            int dot = name.lastIndexOf('.');
            want = (dot > 0 ? name.substring(0, dot) : name) + " (" + n + ")"
                   + (dot > 0 ? name.substring(dot) : "");
        }
        try {
            String srcDir = resolve(dirName(rel), false);
            DocumentsContract.moveDocument(cr, DocumentsContract.buildDocumentUriUsingTree(tree, docId),
                    DocumentsContract.buildDocumentUriUsingTree(tree, srcDir),
                    DocumentsContract.buildDocumentUriUsingTree(tree, destDir));
            if (!want.equals(name)) {
                String moved = childId(destDir, name);
                if (moved != null) DocumentsContract.renameDocument(cr,
                        DocumentsContract.buildDocumentUriUsingTree(tree, moved), want);
            }
        } catch (Exception e) {
            return null;
        }
        return destRel + "/" + want;
    }

    public String sha256(Uri doc) {
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
        } finally { try { if (in != null) in.close(); } catch (Exception ignored) { } }
    }

    public Uri docUri(String docId) {
        return DocumentsContract.buildDocumentUriUsingTree(tree, docId);
    }

    // ------------------------------------------------------------------------------- scanning

    /** What one walk of the tree found: the live files, and what it deliberately did not look at. */
    public static final class Scan {
        /** rel path -> {size, mtime, sha?} — `sha` is the FILE's hash, never a blob address. */
        public final Map<String, Map<String, Object>> files = new LinkedHashMap<String, Map<String, Object>>();
        public final List<Map<String, Object>> skipped = new ArrayList<Map<String, Object>>();
    }

    private static Map<String, Object> skip(String path, String why, long size) {
        Map<String, Object> o = new LinkedHashMap<String, Object>();
        o.put("path", path);
        o.put("why", why);
        if (size >= 0) o.put("size", size);
        return o;
    }

    public Scan scan(boolean hash, long maxBytes, List<String> excludes) {
        Scan out = new Scan();
        String rootDoc = DocumentsContract.getTreeDocumentId(tree);
        ArrayDeque<String[]> queue = new ArrayDeque<String[]>();   // {documentId, relative path}
        queue.add(new String[]{ rootDoc, "" });
        while (!queue.isEmpty()) {
            String[] cur = queue.poll();
            Uri kids = DocumentsContract.buildChildDocumentsUriUsingTree(tree, cur[0]);
            Cursor c = null;
            try {
                c = cr.query(kids, COLS, null, null, null);
                if (c == null) { out.skipped.add(skip(cur[1], "unreadable", -1)); continue; }
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
                    if (maxBytes > 0 && size > maxBytes) {
                        out.skipped.add(skip(rel, "too big", size));
                        continue;
                    }
                    Map<String, Object> e = new LinkedHashMap<String, Object>();
                    e.put("size", size);
                    e.put("mtime", mtime);
                    if (hash) {
                        String sha = sha256(docUri(docId));
                        if (sha == null) { out.skipped.add(skip(rel, "unreadable", -1)); continue; }
                        // A photo still being written by the camera hashes to bytes that were never a
                        // whole file, and a corrupt copy with a valid checksum is worse than a delay.
                        long[] now = statById(docId);
                        if (now == null || now[0] != size || now[1] != mtime) {
                            out.skipped.add(skip(rel, "in use — will try again", -1));
                            continue;
                        }
                        e.put("sha", sha);
                    }
                    out.files.put(rel, e);
                }
            } catch (Exception ex) {
                out.skipped.add(skip(cur[1], "unreadable", -1));
            } finally { if (c != null) c.close(); }
        }
        return out;
    }

    // -------------------------------------------------------------------------- reading bytes

    /**
     * The sha256 of a file, computed HERE and never carried across the bridge.
     *
     * The conflict path needs a file's content identity to decide whether the local copy and the
     * manifest's entry are the same bytes — and it was getting it by reading the whole file into the
     * WebView (`readAll` below, then base64 to cross, then a hash pass). At a photo apiece that is
     * tens of megabytes of renderer memory per conflict; on a folder with 1,927 of them the app died
     * on the first. The scan has always hashed this way, streamed, in `sha256(Uri)` — this exposes
     * the same thing for one path.
     */
    public String sha256Of(String rel) throws Exception {
        String docId = resolve(rel, false);
        if (docId == null) throw new java.io.IOException("not found: " + rel);
        return sha256(docUri(docId));
    }

    public byte[] readAll(String rel) throws Exception {
        String docId = resolve(rel, false);
        if (docId == null) throw new java.io.IOException("not found: " + rel);
        InputStream in = cr.openInputStream(docUri(docId));
        if (in == null) throw new java.io.IOException("cannot open: " + rel);
        try {
            java.io.ByteArrayOutputStream bos = new java.io.ByteArrayOutputStream();
            byte[] buf = new byte[65536];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
            return bos.toByteArray();
        } finally { try { in.close(); } catch (Exception ignored) { } }
    }

    /**
     * One slice of a file.
     *
     * RANDOM ACCESS, NOT skip(). InputStream.skip may skip fewer bytes than asked and gives no way to
     * tell that from a short file, so seeking with it silently reads the wrong offset. A
     * ParcelFileDescriptor gives a real channel position; the stream fallback loops until the offset
     * is genuinely reached.
     */
    public byte[] readRange(String rel, long off, int len) throws Exception {
        String docId = resolve(rel, false);
        if (docId == null) throw new java.io.IOException("not found: " + rel);
        Uri doc = docUri(docId);
        byte[] buf = new byte[Math.max(0, len)];
        int got = 0;
        ParcelFileDescriptor pfd = null;
        try { pfd = cr.openFileDescriptor(doc, "r"); } catch (Exception ignored) { }
        if (pfd != null) {
            try {
                FileInputStream fin = new FileInputStream(pfd.getFileDescriptor());
                try {
                    fin.getChannel().position(off);
                    int n;
                    while (got < buf.length && (n = fin.read(buf, got, buf.length - got)) > 0) got += n;
                } finally { fin.close(); }
            } finally { pfd.close(); }
        } else {
            InputStream in = cr.openInputStream(doc);
            if (in == null) throw new java.io.IOException("cannot open: " + rel);
            try {
                long left = off;
                while (left > 0) {
                    long sk = in.skip(left);
                    if (sk <= 0) { byte[] one = new byte[1]; if (in.read(one) < 0) break; sk = 1; }
                    left -= sk;
                }
                int n;
                while (got < buf.length && (n = in.read(buf, got, buf.length - got)) > 0) got += n;
            } finally { try { in.close(); } catch (Exception ignored) { } }
        }
        if (got == buf.length) return buf;
        byte[] exact = new byte[got];
        System.arraycopy(buf, 0, exact, 0, got);
        return exact;
    }

    // -------------------------------------------------------------------------- writing bytes

    /**
     * Write one slice into `name.pcpart`. Offset 0 creates it (clearing any leftovers from a crash);
     * later offsets seek into it. Nothing appears under the real name until {@link #commitPart}, so
     * an interrupted download leaves a part file and never a half-written document.
     */
    public void writePart(String rel, long off, byte[] bytes) throws Exception {
        String name = baseName(rel), dirRel = dirName(rel);
        String dirId = resolve(dirRel, true);
        if (dirId == null) throw new java.io.IOException("cannot create " + dirRel);

        String partId = childId(dirId, name + PART);
        if (off == 0 && partId != null) { deleteDoc(partId); partId = null; }
        Uri partUri;
        if (partId == null) {
            partUri = DocumentsContract.createDocument(cr, docUri(dirId),
                                                       "application/octet-stream", name + PART);
        } else {
            partUri = docUri(partId);
        }
        if (partUri == null) throw new java.io.IOException("cannot write " + rel);

        // "rw" keeps what is already there; "w" truncates, which would throw away every chunk
        // written before this one.
        ParcelFileDescriptor pfd = cr.openFileDescriptor(partUri, "rw");
        if (pfd == null) throw new java.io.IOException("cannot open " + rel + " for writing");
        try {
            FileOutputStream fos = new FileOutputStream(pfd.getFileDescriptor());
            try {
                fos.getChannel().position(off);
                fos.write(bytes);
                fos.flush();
            } finally { fos.close(); }
        } finally { pfd.close(); }
    }

    /** The sha256 of what has been written so far, or null when there is no part file. */
    public String hashPart(String rel) {
        String dirId = resolve(dirName(rel), false);
        String partId = dirId == null ? null : childId(dirId, baseName(rel) + PART);
        return partId == null ? null : sha256(docUri(partId));
    }

    /** Deleted, never trashed: this is not somebody's file, it is bytes we could not confirm. */
    public void discardPart(String rel) {
        String dirId = resolve(dirName(rel), false);
        String partId = dirId == null ? null : childId(dirId, baseName(rel) + PART);
        if (partId != null) deleteDoc(partId);
    }

    /** How much of an interrupted download is already on disk. 0 when there is nothing to resume. */
    public long partSize(String rel) {
        String dirId = resolve(dirName(rel), false);
        String partId = dirId == null ? null : childId(dirId, baseName(rel) + PART);
        long[] st = partId == null ? null : statById(partId);
        return st == null ? 0 : st[0];
    }

    /**
     * Put the finished part file in place, and answer what it actually became.
     *
     * THE COMMIT HAS TO BE CHECKED, or a failure reports as a successful download. There is no
     * rename-over-an-existing-document in SAF, so the old file is trashed FIRST — and if that move
     * fails the name is still taken, the rename fails too, and childId/statById happily answer with
     * the OLD file's size and mtime, which used to resolve as success. `base` then claimed the remote
     * version was present and the update was never retried: the newer version silently never landed.
     *
     * @return {size, mtime} as the provider reports them — SAF has no writable last-modified, so this
     *         is what the sweep must record as agreed, or the next sweep reads its own download as a
     *         local edit.
     */
    public long[] commitPart(String rel, long when) throws Exception {
        String name = baseName(rel), dirRel = dirName(rel);
        String dirId = resolve(dirRel, true);
        if (dirId == null) throw new java.io.IOException("cannot create " + dirRel);
        String partId = childId(dirId, name + PART);
        if (partId == null) throw new java.io.IOException("nothing to commit for " + rel);

        String existing = childId(dirId, name);
        if (existing != null && trashDoc(existing, rel, when) == null) {
            throw new java.io.IOException("could not clear the previous " + rel
                    + " — refusing to report a commit that did not happen");
        }
        try {
            DocumentsContract.renameDocument(cr, docUri(partId), name);
        } catch (Exception e) {
            throw new java.io.IOException("could not put " + rel + " in place: " + e.getMessage());
        }
        if (childId(dirId, name + PART) != null) {
            throw new java.io.IOException("could not put " + rel + " in place — the part file is still there");
        }
        String finalId = childId(dirId, name);
        if (finalId == null) throw new java.io.IOException("could not put " + rel + " in place");
        long[] st = statById(finalId);
        return new long[]{ st != null ? st[0] : 0, st != null ? st[1] : System.currentTimeMillis() };
    }

    /** Whole-file write: the same landing as the chunked path, so both end up identical on disk. */
    public long[] write(String rel, byte[] bytes, long when) throws Exception {
        String name = baseName(rel), dirRel = dirName(rel);
        String dirId = resolve(dirRel, true);
        if (dirId == null) throw new java.io.IOException("cannot create " + dirRel);

        String partId = childId(dirId, name + PART);
        if (partId != null) deleteDoc(partId);                    // a previous crash's leftovers
        Uri partUri = DocumentsContract.createDocument(cr, docUri(dirId),
                                                       "application/octet-stream", name + PART);
        if (partUri == null) throw new java.io.IOException("cannot write " + rel);
        OutputStream out = cr.openOutputStream(partUri, "w");
        if (out == null) throw new java.io.IOException("cannot open " + rel + " for writing");
        try {
            out.write(bytes);
            out.flush();
        } finally { try { out.close(); } catch (Exception ignored) { } }

        String existing = childId(dirId, name);
        if (existing != null) trashDoc(existing, rel, when);
        DocumentsContract.renameDocument(cr, partUri, name);

        String finalId = childId(dirId, name);
        long[] st = finalId == null ? null : statById(finalId);
        return new long[]{ st != null ? st[0] : bytes.length,
                           st != null ? st[1] : System.currentTimeMillis() };
    }

    /**
     * Move one file into the trash, and answer where it went.
     *
     * A FAILED TRASH IS A FAILURE. Answering "done" for a file still on disk made the sweep agree a
     * tombstone for it, so the next sweep read it as a local edit and RE-UPLOADED it — resurrecting a
     * file another device had deleted, while reporting "1 to trash" both times.
     */
    public String trash(String rel, long when) throws Exception {
        String docId = resolve(rel, false);
        if (docId == null) throw new java.io.IOException("not found: " + rel);
        String dest = trashDoc(docId, rel, when);
        if (dest == null) throw new java.io.IOException("could not move " + rel + " to the trash");
        // Only AFTER the move is known to have succeeded — pruning around a failed trash would remove
        // a directory whose file is still sitting in it.
        pruneEmptyDirs(rel);
        return dest;
    }
}
