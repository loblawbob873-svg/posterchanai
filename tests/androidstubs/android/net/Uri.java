package android.net;

/** Signature-only, with ONE real behaviour: `parse` hands back something non-null, because
 *  ContactWriter.landed() distinguishes an insert that produced a row (a uri) from a provider that
 *  quietly did nothing (neither uri nor count), and a stub that always answered null would make that
 *  test pass against the wrong answer. */
public abstract class Uri {
  public abstract Builder buildUpon();
  public String getLastPathSegment() {
    java.util.List<String> p = getPathSegments();
    return p.isEmpty() ? null : p.get(p.size() - 1);
  }
  /* REAL PARSING, because the Folder Sync crash is decided by the SHAPE of a uri: a `/tree/...`
     grant is a folder and a `/document/...` grant is a file the user once picked, and treating the
     second as the first is what ended the app process. A stub returning an empty list would let
     that distinction pass untested. */
  public java.util.List<String> getPathSegments() {
    String s = toString();
    int i = s.indexOf("://");
    String rest = i < 0 ? s : s.substring(i + 3);
    int slash = rest.indexOf('/');
    if (slash < 0) return java.util.Collections.emptyList();
    java.util.List<String> out = new java.util.ArrayList<>();
    for (String part : rest.substring(slash + 1).split("/")) if (!part.isEmpty()) out.add(part);
    return out;
  }
  public String toString() { return "content://stub"; }
  public static Uri parse(String s) { return new Parsed(s); }
  public abstract static class Builder {
    public abstract Builder appendQueryParameter(String key, String value);
    public abstract Uri build();
  }
  private static final class Parsed extends Uri {
    private final String raw;
    Parsed(String raw) { this.raw = raw == null ? "" : raw; }
    @Override public String toString() { return raw; }
    @Override public Builder buildUpon() { return null; }
  }
}
