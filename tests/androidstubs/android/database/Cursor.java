package android.database;

public interface Cursor {
  boolean moveToNext();
  boolean moveToFirst();
  String getString(int column);
  long getLong(int column);
  int getInt(int column);
  int getCount();
  void close();
}
