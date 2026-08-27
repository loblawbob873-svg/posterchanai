package place.poster.app.home;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Lets the adb emulator gate opt into HOME through the same app-owned API as Settings. Debug only. */
public final class HomeTestReceiver extends BroadcastReceiver {
    public static final String ACTION = "place.poster.app.test.SET_HOME_COMPONENT";
    @Override public void onReceive(Context context, Intent intent) {
        if (intent == null || !ACTION.equals(intent.getAction())) return;
        HomeRoles.enableLauncherComponent(context, intent.getBooleanExtra("enabled", true));
    }
}
