package com.getcapacitor;

import android.content.Context;
import android.content.Intent;

public class Plugin {
  /** Capacitor calls this once the bridge is built; plugins override it. */
  public void load() {}
  public Context getContext() { return null; }
  public Bridge getBridge() { return null; }
  public android.app.Activity getActivity() { return null; }
  public PermissionState getPermissionState(String alias) { return PermissionState.DENIED; }
  protected void requestPermissionForAlias(String alias, PluginCall call, String callbackName) {}
  protected void notifyListeners(String eventName, JSObject data) {}
  protected void startActivityForResult(PluginCall call, Intent intent, String callbackName) {}
  protected void handleOnDestroy() {}
  public void handleOnResume() {}
  public void handleOnPause() {}
}
