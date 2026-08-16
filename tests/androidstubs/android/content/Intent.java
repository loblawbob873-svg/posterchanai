package android.content;

public class Intent {
  public static final String ACTION_BOOT_COMPLETED = "android.intent.action.BOOT_COMPLETED";
  public static final String ACTION_MY_PACKAGE_REPLACED = "android.intent.action.MY_PACKAGE_REPLACED";
  public static final String ACTION_OPEN_DOCUMENT_TREE = "android.intent.action.OPEN_DOCUMENT_TREE";
  public static final int FLAG_GRANT_READ_URI_PERMISSION = 0x00000001;
  public static final int FLAG_GRANT_WRITE_URI_PERMISSION = 0x00000002;
  public static final int FLAG_GRANT_PERSISTABLE_URI_PERMISSION = 0x00000040;
  public static final int FLAG_ACTIVITY_NEW_TASK = 0x10000000;
  public static final int FLAG_ACTIVITY_CLEAR_TOP = 0x04000000;
  public Intent() { }
  public Intent(String action) { }
  public Intent(Context ctx, Class<?> cls) { }
  public Intent setAction(String action) { return this; }
  public String getAction() { return null; }
  public Intent addFlags(int flags) { return this; }
  public android.net.Uri getData() { return null; }
  public Intent setFlags(int flags) { return this; }
}
