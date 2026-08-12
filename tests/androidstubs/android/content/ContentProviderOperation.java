package android.content;

import android.net.Uri;

public class ContentProviderOperation {
  public static Builder newInsert(Uri uri) { return null; }
  public static Builder newUpdate(Uri uri) { return null; }
  public static Builder newDelete(Uri uri) { return null; }

  public static class Builder {
    public Builder withValue(String key, Object value) { return this; }
    public Builder withValueBackReference(String key, int previousResult) { return this; }
    public Builder withSelection(String selection, String[] args) { return this; }
    public ContentProviderOperation build() { return null; }
  }
}
