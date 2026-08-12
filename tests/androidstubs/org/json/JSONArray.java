package org.json;

public class JSONArray {
  public JSONArray() {}
  public JSONArray(String source) throws JSONException {}
  public JSONArray put(Object value) { return this; }
  public JSONArray put(long value) { return this; }
  public int length() { return 0; }
  public Object opt(int index) { return null; }
  public JSONObject optJSONObject(int index) { return null; }
  public String toString() { return "[]"; }
}
