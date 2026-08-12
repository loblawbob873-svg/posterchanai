package android.net;

public abstract class Uri {
  public abstract Builder buildUpon();
  public static Uri parse(String s) { return null; }
  public abstract static class Builder {
    public abstract Builder appendQueryParameter(String key, String value);
    public abstract Uri build();
  }
}
