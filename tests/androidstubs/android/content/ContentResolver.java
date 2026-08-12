package android.content;

import android.accounts.Account;
import android.database.Cursor;
import android.net.Uri;

import java.util.ArrayList;

public abstract class ContentResolver {
  public abstract Cursor query(Uri uri, String[] projection, String selection,
                               String[] selectionArgs, String sortOrder);
  public abstract int delete(Uri uri, String selection, String[] selectionArgs);
  public abstract int update(Uri uri, ContentValues values, String selection, String[] args);
  /** The real one throws RemoteException and OperationApplicationException — both checked. */
  public abstract ContentProviderResult[] applyBatch(String authority,
      ArrayList<ContentProviderOperation> operations) throws Exception;

  public static void setIsSyncable(Account account, String authority, int syncable) {}
  public static void setSyncAutomatically(Account account, String authority, boolean sync) {}
}
