package android.net;

/** Signature-only, with ONE real behaviour: `parse` hands back something non-null, because
 *  ContactWriter.landed() distinguishes an insert that produced a row (a uri) from a provider that
 *  quietly did nothing (neither uri nor count), and a stub that always answered null would make that
 *  test pass against the wrong answer. */
public abstract class Uri {
  public abstract Builder buildUpon();
  public String getLastPathSegment() { return null; }
  public String toString() { return "content://stub"; }
  public static Uri parse(String s) { return new Parsed(); }
  public abstract static class Builder {
    public abstract Builder appendQueryParameter(String key, String value);
    public abstract Uri build();
  }
  private static final class Parsed extends Uri {
    @Override public Builder buildUpon() { return null; }
  }
}
