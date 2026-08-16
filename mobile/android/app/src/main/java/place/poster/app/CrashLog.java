package place.poster.app;

import android.content.Context;

import java.io.File;
import java.io.FileOutputStream;
import java.io.FileInputStream;
import java.io.PrintWriter;
import java.io.StringWriter;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * THE STACK TRACE OF THE CRASH THAT ALREADY HAPPENED, KEPT WHERE SOMEBODY WITHOUT A CABLE CAN READ
 * IT.
 *
 * Android shows "PosterChan has stopped", writes the trace to logcat, and drops it. Reading logcat
 * needs `adb`, a cable, and developer options — and the person who hits this crash has none of
 * those. So four rounds of "fixed" were spent on this feature reasoning from a SYMPTOM DESCRIPTION,
 * each round costing an APK build, a deploy and somebody's evening, and each one wrong: the guesses
 * were guarded call sites, and the throw was somewhere none of those guards could reach. That is not
 * a hard bug. It is a bug nobody has ever seen.
 *
 * WHAT MAKES IT WORTH A FILE RATHER THAN A CRASH REPORTER. There is no Crashlytics here and there
 * should not be: this app is self-hosted, the whole point is that nothing phones home, and a crash
 * trace names files and folders. So it stays on the device, in this app's private storage, and it
 * goes nowhere unless the user copies it out of the Background details panel themselves.
 *
 * THE HANDLER MUST NOT BE THE LAST WORD. It records and then DELEGATES to the handler it replaced,
 * which on Android is the platform's own — the one that shows the dialog and ends the process.
 * Swallowing the exception instead would leave the process alive with a dead main looper: a frozen
 * app, no dialog, nothing in logcat, which is strictly worse than the crash it replaced and is the
 * classic way a crash reporter makes an app harder to debug rather than easier.
 *
 * IT IS INSTALLED FROM {@link PosterChanApp}, NOT FROM THE ACTIVITY, and that is the difference
 * between catching this bug and not. The crash being chased happens while the app is BACKGROUNDED,
 * and this process can be started with no Activity at all — an alarm firing into
 * {@code SyncTickReceiver} starts it cold. A handler installed in {@code MainActivity.onCreate} is
 * simply not installed on that path, which is precisely the path under investigation.
 * {@code Application.onCreate} runs for every process start there is.
 *
 * NOTHING IN HERE MAY THROW. A handler that throws while handling replaces a diagnosable crash with
 * a recursive one, so every step is guarded and re-entrancy is refused outright.
 */
public final class CrashLog {

  private CrashLog() { }

  private static final String FILE = "crash-log.txt";
  /** Enough to show a pattern — "it dies every time the screen goes off" is itself the diagnosis —
   *  without letting a crash loop fill the disk. */
  private static final int KEEP = 3;
  private static final int CAP = 24 * 1024;
  private static final String HEAD = "PosterChan crash";

  private static volatile boolean installed = false;
  private static volatile boolean writing = false;
  private static volatile String version = "";

  /**
   * @param versionName passed IN rather than read here, so this class needs no PackageManager and
   *                    can therefore be run in a test on a desktop JVM. The build number is the
   *                    first thing anybody reading a report has to know, because a fix that is not
   *                    in the build on the phone explains every "still broken" by itself.
   */
  public static void install(Context ctx, String versionName) {
    if (installed || ctx == null) return;
    installed = true;
    version = versionName == null ? "" : versionName;
    final Context app = ctx.getApplicationContext();
    final Thread.UncaughtExceptionHandler prev = Thread.getDefaultUncaughtExceptionHandler();
    Thread.setDefaultUncaughtExceptionHandler(new Thread.UncaughtExceptionHandler() {
      public void uncaughtException(Thread t, Throwable e) {
        try {
          if (!writing) { writing = true; record(app, t, e); }
        } catch (Throwable ignored) {
          // A failure to RECORD a crash must never become the crash. Fall through and let the
          // platform handle the original one, which is the outcome we had before this file existed.
        } finally {
          writing = false;
          /* Android's RuntimeInit always installs KillApplicationHandler, so `prev` is null only
           * off-device (a plain JVM, i.e. the test). On-device this line is what shows the dialog
           * and ends the process; there is deliberately no `killProcess` fallback, because the only
           * situation that could reach it is one where nothing was going to die anyway. */
          if (prev != null) prev.uncaughtException(t, e);
        }
      }
    });
  }

  /** Visible for the test: the recording half, without having to actually crash a JVM. */
  static void record(Context ctx, Thread t, Throwable e) {
    StringWriter sw = new StringWriter();
    PrintWriter pw = new PrintWriter(sw);
    pw.println(HEAD + " · " + version + " · " + stamp());
    pw.println("thread: " + (t == null ? "?" : t.getName()) + " · " + facts());
    if (e != null) e.printStackTrace(pw);
    pw.flush();
    prepend(ctx, sw.toString());
  }

  /**
   * WHAT THE PHONE WAS DOING WHEN IT DIED, which is half of what makes a trace actionable here.
   *
   * A trace names the line; it does not say whether the app was on screen, whether a sweep was in
   * flight, or whether the foreground-service start had been refused all day — and every competing
   * explanation for this crash differs on exactly those. Each fact is read in its own guard: these
   * are static reads across a package boundary during a crash, and one unlucky class-initialiser
   * must not cost the trace it is annotating.
   */
  private static String facts() {
    StringBuilder b = new StringBuilder();
    try {
      b.append("app ")
       .append(place.poster.app.sync.FolderSyncPlugin.appInForeground() ? "on screen" : "backgrounded");
    } catch (Throwable ignored) { b.append("app ?"); }
    try {
      b.append(" · sync service ").append(place.poster.app.sync.SyncService.running ? "up" : "down");
    } catch (Throwable ignored) { }
    try {
      b.append(" · native sweep ").append(place.poster.app.sync.NativeRunner.busy() ? "running" : "idle");
    } catch (Throwable ignored) { }
    try {
      b.append(" · clock fired ").append(place.poster.app.sync.SyncClock.firedCount())
       .append(", fg ").append(place.poster.app.sync.SyncClock.foregroundCount())
       .append(", job ").append(place.poster.app.sync.SyncClock.jobCount())
       .append(", refused ").append(place.poster.app.sync.SyncClock.refusedCount());
    } catch (Throwable ignored) { }
    return b.toString();
  }

  private static String stamp() {
    try {
      return new SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US).format(new Date());
    } catch (Throwable ignored) { return "?"; }
  }

  /**
   * NEWEST FIRST, and that ordering is the whole ergonomics of this file: the panel shows the top of
   * it, and the crash somebody is asking about is the one that just happened. Older entries are kept
   * because a crash that repeats identically and a crash that changes shape are different bugs.
   */
  private static void prepend(Context ctx, String entry) {
    File f = file(ctx);
    if (f == null) return;
    String old = read(ctx);
    String all = trim(entry + (old.isEmpty() ? "" : "\n" + old));
    FileOutputStream out = null;
    try {
      out = new FileOutputStream(f, false);
      out.write(all.getBytes("UTF-8"));
      out.flush();
    } catch (Throwable ignored) {
    } finally {
      if (out != null) try { out.close(); } catch (Throwable ignored) { }
    }
  }

  /** Bound by ENTRIES first and bytes second — a single enormous trace must still be kept whole, or
   *  the one report we get is the one that was truncated before the interesting frames. */
  private static String trim(String all) {
    int cut = -1, seen = 0;
    for (int i = 0; i >= 0 && i < all.length(); ) {
      int at = all.indexOf(HEAD + " · ", i);
      if (at < 0) break;
      seen++;
      if (seen > KEEP) { cut = at; break; }
      i = at + HEAD.length();
    }
    if (cut > 0) all = all.substring(0, cut);
    if (all.length() > CAP) {
      // Keep the NEWEST. A tail cut on a newest-first file drops the oldest entry, which is the
      // right one to lose.
      int at = all.lastIndexOf(HEAD + " · ", CAP);
      all = at > 0 ? all.substring(0, at) : all.substring(0, CAP);
    }
    return all;
  }

  /** @return the log, newest crash first, or "" when this phone has never crashed. Never null. */
  public static String read(Context ctx) {
    File f = file(ctx);
    if (f == null || !f.exists()) return "";
    FileInputStream in = null;
    try {
      in = new FileInputStream(f);
      byte[] buf = new byte[(int) Math.min(f.length(), (long) CAP * 2)];
      int n = 0, r;
      while (n < buf.length && (r = in.read(buf, n, buf.length - n)) > 0) n += r;
      return new String(buf, 0, n, "UTF-8");
    } catch (Throwable ignored) {
      return "";
    } finally {
      if (in != null) try { in.close(); } catch (Throwable ignored) { }
    }
  }

  public static void clear(Context ctx) {
    File f = file(ctx);
    try { if (f != null && f.exists()) f.delete(); } catch (Throwable ignored) { }
  }

  private static File file(Context ctx) {
    try {
      File dir = ctx.getFilesDir();
      return dir == null ? null : new File(dir, FILE);
    } catch (Throwable ignored) { return null; }
  }
}
