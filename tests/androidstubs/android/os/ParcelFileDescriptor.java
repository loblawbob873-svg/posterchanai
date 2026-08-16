package android.os;

import java.io.FileDescriptor;

/** Signature-only — see android/provider/DocumentsContract.java. */
public class ParcelFileDescriptor implements java.io.Closeable {
  public FileDescriptor getFileDescriptor() { return null; }
  public void close() throws java.io.IOException {}
}
