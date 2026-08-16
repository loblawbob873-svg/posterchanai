package android.provider;

import android.content.ContentResolver;
import android.net.Uri;

/** Signature-only. Present so tests/test_android_native_sync_saf.py can TYPE-CHECK SafFs here — a
 *  wrong column constant or a dropped argument is then a failing test in a second, rather than a
 *  broken APK build on CI. Nothing below runs. */
public final class DocumentsContract {
  private DocumentsContract() {}

  public static final class Document {
    public static final String COLUMN_DOCUMENT_ID = "document_id";
    public static final String COLUMN_DISPLAY_NAME = "_display_name";
    public static final String COLUMN_MIME_TYPE = "mime_type";
    public static final String COLUMN_SIZE = "_size";
    public static final String COLUMN_LAST_MODIFIED = "last_modified";
    public static final String MIME_TYPE_DIR = "vnd.android.document/directory";
  }

  public static String getTreeDocumentId(Uri tree) { return null; }
  public static String getDocumentId(Uri doc) { return null; }
  public static Uri buildChildDocumentsUriUsingTree(Uri tree, String parentDocumentId) { return null; }
  public static Uri buildDocumentUriUsingTree(Uri tree, String documentId) { return null; }
  /** Checked FileNotFoundException on the real one — SafFs.resolve depends on catching it. */
  public static Uri createDocument(ContentResolver cr, Uri parent, String mime, String name)
      throws java.io.FileNotFoundException { return null; }
  public static boolean deleteDocument(ContentResolver cr, Uri doc)
      throws java.io.FileNotFoundException { return false; }
  public static Uri renameDocument(ContentResolver cr, Uri doc, String newName)
      throws java.io.FileNotFoundException { return null; }
  public static Uri moveDocument(ContentResolver cr, Uri doc, Uri parent, Uri target)
      throws java.io.FileNotFoundException { return null; }
}
