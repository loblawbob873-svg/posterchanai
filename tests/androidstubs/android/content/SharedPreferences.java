package android.content;

public interface SharedPreferences {
  long getLong(String key, long dflt);
  String getString(String key, String def);
  boolean getBoolean(String key, boolean def);
  Editor edit();

  interface Editor {
    Editor putLong(String key, long value);
    Editor putString(String key, String value);
    Editor putBoolean(String key, boolean value);
    Editor remove(String key);
    boolean commit();
    void apply();
  }
}
