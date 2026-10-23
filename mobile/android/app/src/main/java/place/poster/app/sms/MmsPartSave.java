package place.poster.app.sms;

import android.content.ContentResolver;

import java.io.InputStream;

import place.poster.app.preview.MediaSaveWriter;

/**
 * SAVE ONE ATTACHMENT OF A PICTURE MESSAGE TO THE PHONE ("sms apk does not let me save media
 * attachments"). The Messages screen drew a picture you could only look at, a video you could only
 * play, and no sound or file at all -- its long-press menu was react / copy / delete. This is the Save.
 *
 * The bytes come straight off `content://mms/part/<id>` and are streamed into the same Downloads/
 * PosterChan tree the image viewer's Save writes (MediaSaveWriter), so there is one place to look.
 * It answers the SENTENCE to show: where it went, or why it did not -- never silence.
 */
final class MmsPartSave {
    private MmsPartSave() { }

    /** Something a person can save: not the words (they are the body) and not the SMIL layout. */
    static boolean savable(SmsPart p) {
        return p != null && !p.isText() && !p.isSmil();
    }

    /** "Photo" / "Video" / "Audio", or the sender's file name for anything else. */
    static String label(SmsPart p) {
        String ct = p == null || p.ct == null ? "" : p.ct.toLowerCase(java.util.Locale.ROOT);
        if (ct.startsWith("image/")) return "Photo";
        if (ct.startsWith("video/")) return "Video";
        if (ct.startsWith("audio/")) return "Audio";
        if (ct.contains("vcard")) return "Contact card";
        return p == null || p.name == null || p.name.isEmpty() ? "Attachment" : p.name;
    }

    /** The sender's file name when it gave one; otherwise a name that says what it is. */
    static String fileName(SmsPart p) {
        String n = p == null || p.name == null ? "" : p.name.trim();
        if (!n.isEmpty()) return n;
        return "mms-" + label(p).toLowerCase(java.util.Locale.ROOT).replace(' ', '-') + "-" + (p == null ? 0 : p.id);
    }

    /** Writes the part and answers what to tell the person. Closes `in`. */
    static String save(ContentResolver cr, InputStream in, SmsPart p) {
        String label = label(p);
        try {
            if (in == null) return "Could not save " + label + ": the phone would not open it";
            String where = MediaSaveWriter.write(cr, in, fileName(p), p.ct, "");
            return label + " saved to " + where;
        } catch (Throwable t) {
            String why = t.getMessage();
            return "Could not save " + label + ": " + (why == null || why.isEmpty() ? "error" : why);
        } finally {
            if (in != null) try { in.close(); } catch (Throwable ignored) { }
        }
    }
}
