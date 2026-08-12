package com.getcapacitor;

public class PluginCall {
  public String getString(String name) { return null; }
  public String getString(String name, String def) { return def; }
  public Boolean getBoolean(String name, Boolean def) { return def; }
  public JSArray getArray(String name) { return null; }
  public JSObject getObject(String name) { return null; }
  public void resolve() {}
  public void resolve(JSObject data) {}
  public void reject(String message) {}
}
