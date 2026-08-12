package com.getcapacitor;

import org.json.JSONObject;

/** Capacitor's JSObject swallows JSONException on put() and returns itself, so bridge code can
 *  chain — which is why the app's plugin methods declare no `throws`. */
public class JSObject extends JSONObject {
  public JSObject() {}
  @Override public JSObject put(String name, Object value) { return this; }
  public JSObject put(String name, String value) { return this; }
  public JSObject put(String name, boolean value) { return this; }
  public JSObject put(String name, int value) { return this; }
  public JSObject put(String name, long value) { return this; }
}
