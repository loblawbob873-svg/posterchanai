package place.poster.app;

import android.app.Application;
import android.content.pm.PackageInfo;

/**
 * THE FIRST CODE THIS APP RUNS, in every process start there is.
 *
 * It exists for one reason: {@link CrashLog} has to be installed before anything can crash, and the
 * crash being chased happens with NO ACTIVITY IN EXISTENCE — an alarm firing into
 * {@code SyncTickReceiver} starts this process cold, sweeps in a service, and dies there. Installing
 * the handler in {@code MainActivity.onCreate} would cover every path except the one under
 * investigation, and would have looked exactly as correct.
 *
 * Nothing else belongs here. Application.onCreate runs before the launcher icon has finished
 * animating and on every background wake-up, so work put here is paid for by paths that need none
 * of it.
 */
public class PosterChanApp extends Application {

  @Override
  public void onCreate() {
    super.onCreate();
    /* The build number, read here rather than inside CrashLog so that class stays free of Android
     * plumbing and can be RUN in a test. Guarded because a report with no version is still worth
     * having, and because getPackageInfo on our own package failing would otherwise take the whole
     * app down at launch — a crash reporter that crashes the app is the joke that writes itself. */
    String v = "";
    try {
      PackageInfo pi = getPackageManager().getPackageInfo(getPackageName(), 0);
      if (pi != null && pi.versionName != null) v = pi.versionName;
    } catch (Throwable ignored) { }
    try { CrashLog.install(this, v); } catch (Throwable ignored) { }
  }
}
