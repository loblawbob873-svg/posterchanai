package com.getcapacitor;

public class PluginCall {
  public String getString(String name) { return null; }
  public String getString(String name, String def) { return def; }
  public Boolean getBoolean(String name, Boolean def) { return def; }
  public Integer getInt(String name) { return null; }
  public Integer getInt(String name, Integer def) { return def; }
  public Long getLong(String name) { return null; }
  public Long getLong(String name, Long def) { return def; }
  public Double getDouble(String name) { return null; }
  public Double getDouble(String name, Double def) { return def; }
  public JSArray getArray(String name) { return null; }
  public JSArray getArray(String name, JSArray def) { return def; }
  public JSObject getObject(String name) { return null; }
  public void resolve() {}
  public void resolve(JSObject data) {}
  public void reject(String message) {}
  public void reject(String message, Exception e) {}
}
