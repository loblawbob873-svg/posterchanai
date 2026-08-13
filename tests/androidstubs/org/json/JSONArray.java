package org.json;

public class JSONArray {
  public JSONArray() {}
  public JSONArray(String source) throws JSONException {}
  public JSONArray put(Object value) { return this; }
  public JSONArray put(long value) { return this; }
  public JSONArray put(int value) { return this; }
  /* THROWS, exactly as Android's does — it rejects NaN and Infinity. The stub used to accept a
     double silently through put(Object), so code that needed a catch compiled here and failed on
     CI. A stub that is more permissive than the platform is worse than no stub: it converts a
     local, instant error into a four-minute round trip through a release build. */
  public JSONArray put(double value) throws JSONException { return this; }
  public int length() { return 0; }
  public Object opt(int index) { return null; }
  public JSONObject optJSONObject(int index) { return null; }
  public String toString() { return "[]"; }
}
