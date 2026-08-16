package android.content;

import android.accounts.Account;
import android.database.Cursor;
import android.net.Uri;

import java.util.ArrayList;

public abstract class ContentResolver {
  /** The document-provider side, for SafFs. All three throw a CHECKED FileNotFoundException on
   *  the real class, which is why the callers wrap them. */
  public java.io.InputStream openInputStream(Uri uri) throws java.io.FileNotFoundException { return null; }
  public java.io.OutputStream openOutputStream(Uri uri, String mode) throws java.io.FileNotFoundException { return null; }
  public android.os.ParcelFileDescriptor openFileDescriptor(Uri uri, String mode)
      throws java.io.FileNotFoundException { return null; }

  public abstract Cursor query(Uri uri, String[] projection, String selection,
                               String[] selectionArgs, String sortOrder);
  public abstract int delete(Uri uri, String selection, String[] selectionArgs);
  public abstract int update(Uri uri, ContentValues values, String selection, String[] args);
  /** The real one throws RemoteException and OperationApplicationException — both checked. */
  public abstract ContentProviderResult[] applyBatch(String authority,
      ArrayList<ContentProviderOperation> operations) throws Exception;

  /**
   * Test control: this phone's app has no WRITE_SYNC_SETTINGS.
   *
   * Both calls below REQUIRE that permission and throw SecurityException without it — which is what
   * they did on every real device, undeclared, for the whole life of the contacts feature. Modelled
   * here because the damage was never in the sync settings: it was that a caller turned the throw
   * into "there is no account", and stopped writing contacts to a phone that had one.
   */
  public static boolean syncSettingsDenied = false;

  private static void syncPermission() {
    if (syncSettingsDenied) {
      throw new SecurityException("no permission to write the sync settings");
    }
  }

  public static void setIsSyncable(Account account, String authority, int syncable) {
    syncPermission();
  }

  public static void setSyncAutomatically(Account account, String authority, boolean sync) {
    syncPermission();
  }
}
