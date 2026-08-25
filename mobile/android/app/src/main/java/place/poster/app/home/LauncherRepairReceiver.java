package place.poster.app.home;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** Reasserts an explicitly enabled native HomeActivity after this APK is upgraded. */
public final class LauncherRepairReceiver extends BroadcastReceiver {
    @Override public void onReceive(Context context, Intent intent) {
        if (context == null || intent == null) return;
        if (Intent.ACTION_MY_PACKAGE_REPLACED.equals(intent.getAction())) {
            HomeRoles.repairOptedInLauncher(context);
        }
    }
}
