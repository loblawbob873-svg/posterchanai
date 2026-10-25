package place.poster.app.health;

import android.app.ActivityManager;
import android.app.ApplicationExitInfo;
import android.content.Context;
import android.os.Build;
import android.os.Debug;

import com.getcapacitor.JSArray;
import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.util.List;

/**
 * Settings -> Phone -> "Restarts & memory": WHY the app last ended, and how much memory the device has now.
 *
 * A silent fresh start (no "ran low on memory" / "display error" toast) is the whole process ending, and only
 * Android knows why -- ApplicationExitInfo records the reason, whether the app was on screen, and its memory at
 * that moment. Guessing at it cost several APK rounds before (desk hibernation, compositor layers, window leaks:
 * each a real fix, none proven to be THE cause). See ExitReasons for the labels.
 */
@CapacitorPlugin(name = "AppHealth")
public class AppHealthPlugin extends Plugin {

    @PluginMethod
    public void report(PluginCall call) {
        JSObject o = new JSObject();
        try {
            Context ctx = getContext();
            ActivityManager am = (ActivityManager) ctx.getSystemService(Context.ACTIVITY_SERVICE);
            o.put("sdk", Build.VERSION.SDK_INT);
            o.put("device", Build.MANUFACTURER + " " + Build.MODEL);
            if (am != null) {
                ActivityManager.MemoryInfo mi = new ActivityManager.MemoryInfo();
                am.getMemoryInfo(mi);
                JSObject mem = new JSObject();
                mem.put("totalMb", mi.totalMem / (1024 * 1024));
                mem.put("availMb", mi.availMem / (1024 * 1024));
                mem.put("thresholdMb", mi.threshold / (1024 * 1024));
                mem.put("lowMemory", mi.lowMemory);
                mem.put("memoryClassMb", am.getMemoryClass());
                mem.put("largeMemoryClassMb", am.getLargeMemoryClass());
                mem.put("lowRamDevice", am.isLowRamDevice());
                Debug.MemoryInfo dm = new Debug.MemoryInfo();
                Debug.getMemoryInfo(dm);
                mem.put("appPssMb", dm.getTotalPss() / 1024);
                o.put("memory", mem);
                JSArray exits = new JSArray();
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
                    List<ApplicationExitInfo> list = am.getHistoricalProcessExitReasons(ctx.getPackageName(), 0, 12);
                    for (ApplicationExitInfo e : list) {
                        JSObject x = new JSObject();
                        x.put("at", e.getTimestamp());
                        x.put("reason", ExitReasons.reason(e.getReason()));
                        x.put("unexpected", ExitReasons.unexpected(e.getReason()));
                        x.put("importance", ExitReasons.importance(e.getImportance()));
                        x.put("onScreen", ExitReasons.onScreen(e.getImportance()));
                        x.put("process", e.getProcessName());
                        x.put("pssMb", e.getPss() / 1024);
                        x.put("rssMb", e.getRss() / 1024);
                        x.put("status", e.getStatus());
                        String desc = e.getDescription();
                        x.put("description", desc == null ? "" : (desc.length() > 300 ? desc.substring(0, 300) : desc));
                        exits.put(x);
                    }
                    o.put("exitsSupported", true);
                } else {
                    o.put("exitsSupported", false);
                }
                o.put("exits", exits);
            }
            o.put("rendererDeaths", place.poster.app.MainActivity.RENDERER_DEATHS.get());
            call.resolve(o);
        } catch (Throwable t) {
            call.reject("could not read the app's health: " + t);
        }
    }
}
