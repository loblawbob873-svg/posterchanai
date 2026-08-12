package android.content;

import android.net.Uri;

/** Signature-only. The real one exposes exactly these two public final fields, and which of them is
 *  set is how a caller tells an operation that landed from one the provider quietly ignored. */
public class ContentProviderResult {
  public Uri uri;
  public Integer count;

  public ContentProviderResult() {}
  public ContentProviderResult(Uri uri) { this.uri = uri; }
  public ContentProviderResult(int count) { this.count = Integer.valueOf(count); }
}
