package com.getcapacitor;

import android.content.Context;

public class Plugin {
  /** Capacitor calls this once the bridge is built; plugins override it. */
  public void load() {}
  public Context getContext() { return null; }
  public PermissionState getPermissionState(String alias) { return PermissionState.DENIED; }
  protected void requestPermissionForAlias(String alias, PluginCall call, String callbackName) {}
  protected void notifyListeners(String eventName, JSObject data) {}
}
