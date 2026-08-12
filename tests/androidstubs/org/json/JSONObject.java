package org.json;

import java.util.Iterator;

public class JSONObject {
  public JSONObject() {}
  public JSONObject(String source) throws JSONException {}
  public JSONObject put(String name, Object value) throws JSONException { return this; }
  public Object remove(String name) { return null; }
  public boolean has(String name) { return false; }
  public Iterator<String> keys() { return null; }
  public int length() { return 0; }
  public String optString(String name, String fallback) { return fallback; }
  public long optLong(String name, long fallback) { return fallback; }
  public int optInt(String name, int fallback) { return fallback; }
  public boolean optBoolean(String name, boolean fallback) { return fallback; }
  public JSONObject optJSONObject(String name) { return null; }
  public JSONArray optJSONArray(String name) { return null; }
  public String toString() { return "{}"; }
}
