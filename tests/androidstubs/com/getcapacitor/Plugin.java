package com.getcapacitor;

import android.content.Context;

public class Plugin {
  public Context getContext() { return null; }
  public PermissionState getPermissionState(String alias) { return PermissionState.DENIED; }
  protected void requestPermissionForAlias(String alias, PluginCall call, String callbackName) {}
  protected void notifyListeners(String eventName, JSObject data) {}
}
