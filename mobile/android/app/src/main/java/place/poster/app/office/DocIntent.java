package place.poster.app.office;

import java.util.Locale;

/**
 * WHAT AN INCOMING "OPEN WITH" / "SHARE" / "EDIT" IS, decided without touching Android.
 *
 * PosterChan registers as a handler for office documents and PDFs (the "PosterChan Office" entry
 * in the Open-with sheet, which Android lets a person make the default). This class is the one
 * rule for which of those arrivals is ours and where it goes: a PDF goes to Preview, a document to
 * the Office editor, anything else is not ours and is left to the existing share handling. Pure
 * Java on purpose, so tests/test_android_open_documents.py compiles and RUNS it.
 *
 * The MIME type alone is not enough: file managers routinely send `application/octet-stream` for a
 * .docx, so the file name's extension is the second opinion. And the name alone is not enough
 * either -- a content:// URI often has no name at all until the provider is asked.
 */
public final class DocIntent {
    private DocIntent() { }

    public static final String OFFICE = "office", PDF = "pdf", NONE = "";

    /** The types in the manifest's filters. Keep in step with AndroidManifest.xml (the test checks). */
    public static final String[] OFFICE_MIMES = {
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.presentation",
        "application/rtf",
        "text/rtf",
    };
    public static final String[] OFFICE_EXTS = {
        "doc", "docx", "xls", "xlsx", "ppt", "pptx", "odt", "ods", "odp", "rtf",
    };

    /** OFFICE, PDF or NONE for a type and (optional) file name. */
    public static String kindOf(String mime, String name) {
        String m = mime == null ? "" : mime.toLowerCase(Locale.ROOT).trim();
        int semi = m.indexOf(';');
        if (semi >= 0) m = m.substring(0, semi).trim();
        if ("application/pdf".equals(m)) return PDF;
        for (String o : OFFICE_MIMES) if (o.equals(m)) return OFFICE;
        String ext = extension(name);
        if ("pdf".equals(ext)) return PDF;
        for (String e : OFFICE_EXTS) if (e.equals(ext)) return OFFICE;
        return NONE;
    }

    /**
     * Whether this arrival is one we open as a document. A VIEW or EDIT of a document or PDF always
     * is (that is the Open-with sheet). A SEND is only when it came through the Office entry of the
     * share sheet -- a PDF shared to plain "PosterChan" is still a file to POST, as it always was.
     */
    public static boolean isOpen(String action, boolean viaOfficeEntry, String mime, String name) {
        if (NONE.equals(kindOf(mime, name))) return false;
        if ("android.intent.action.VIEW".equals(action) || "android.intent.action.EDIT".equals(action)) return true;
        return viaOfficeEntry && "android.intent.action.SEND".equals(action);
    }

    public static String extension(String name) {
        if (name == null) return "";
        String n = name.trim();
        int q = n.indexOf('?');
        if (q >= 0) n = n.substring(0, q);
        int slash = Math.max(n.lastIndexOf('/'), n.lastIndexOf('\\'));
        if (slash >= 0) n = n.substring(slash + 1);
        int dot = n.lastIndexOf('.');
        return dot <= 0 || dot == n.length() - 1 ? "" : n.substring(dot + 1).toLowerCase(Locale.ROOT);
    }

    /** A name to show and to save under: the provider's, else the path's last segment, else a default by kind. */
    public static String nameFor(String displayName, String lastPathSegment, String kind) {
        String n = displayName == null ? "" : displayName.trim();
        if (n.isEmpty()) n = lastPathSegment == null ? "" : lastPathSegment.trim();
        n = n.replaceAll("[\\\\/:*?\"<>|\\p{Cntrl}]", "_");
        if (n.isEmpty() || ".".equals(n) || "..".equals(n)) n = PDF.equals(kind) ? "document.pdf" : "document";
        return n.length() > 120 ? n.substring(n.length() - 120) : n;
    }
}
