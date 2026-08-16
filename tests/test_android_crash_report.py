"""The crash log, RUN rather than grepped.

WHY THIS FILE EXISTS. "The app closes when the screen goes off" has been fixed four times from a
symptom description, because the stack trace goes to logcat and reading logcat needs adb, a cable and
developer options — none of which the person hitting the crash has. Every diagnosis was reasoning
about which guarded call site might throw, and every one was wrong. CrashLog writes the trace to a
file the Background details panel can copy, which is the difference between a bug nobody has seen and
a bug with a line number.

A crash reporter is also the one component whose own failure modes are silent by construction: if it
swallows the exception the app freezes instead of dying (no dialog, nothing in logcat, strictly
worse), and if it throws while recording it replaces a diagnosable crash with a recursive one. So the
handler is RUN here — installed, fired, and observed — not matched for text.
"""
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android", "app")
JAVA = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app")
STUBS = os.path.join(ROOT, "tests", "androidstubs")

# CrashLog reads a handful of sync statics to annotate the trace with what the phone was doing, so
# those compile too — the same set the sync state tests build.
SRC = [os.path.join(JAVA, "CrashLog.java")] + \
      [os.path.join(JAVA, "sync", f + ".java") for f in
       ("SyncStore", "NativeSweep", "SyncDiff", "Json", "Excludes", "SyncCrypto",
        "NativeRunner", "FolderSyncPlugin", "SyncClock", "SyncCheckWorker", "SafFs", "SyncNet",
        "SyncWork", "SyncService", "SyncTickReceiver")]
# RunningNote is deliberately NOT here: tests/androidstubs carries a stub of it (the real one reaches
# into the signer and stay-connected services), and javac's default source path is the class path, so
# the stub is what the sync sources compile against — the same arrangement the sync state tests use.

# A Context whose files directory is a real, per-run temp dir, so the log can be written and read
# back for real rather than mocked.
FAKE = """
package place.poster.app;

import android.content.Context;
import android.content.ContentResolver;
import android.content.SharedPreferences;
import java.util.HashMap;
import java.util.Map;

public class Fake extends Context {
  public static final Map<String, Object> STORE = new HashMap<String, Object>();
  public ContentResolver getContentResolver() { return null; }
  public java.io.File getFilesDir() { return new java.io.File(System.getProperty("pc.files")); }
  public SharedPreferences getSharedPreferences(String name, int mode) {
    return new SharedPreferences() {
      public long getLong(String k, long d) { Object v = STORE.get(k); return v == null ? d : (Long) v; }
      public String getString(String k, String d) { Object v = STORE.get(k); return v == null ? d : (String) v; }
      public boolean getBoolean(String k, boolean d) { Object v = STORE.get(k); return v == null ? d : (Boolean) v; }
      public Editor edit() {
        return new Editor() {
          public Editor putLong(String k, long v) { STORE.put(k, Long.valueOf(v)); return this; }
          public Editor putString(String k, String v) { STORE.put(k, v); return this; }
          public Editor putBoolean(String k, boolean v) { STORE.put(k, Boolean.valueOf(v)); return this; }
          public Editor remove(String k) { STORE.remove(k); return this; }
          public void apply() { }
        };
      }
    };
  }
}
"""


def _need(*tools):
    for t in tools:
        if shutil.which(t) is None:
            pytest.skip("no " + t)


def run_java(body):
    """Compile CrashLog + a fake Context + a driver, run it, return stdout."""
    _need("javac", "java")
    with tempfile.TemporaryDirectory() as tmp:
        files = os.path.join(tmp, "files")
        os.makedirs(files)
        with open(os.path.join(tmp, "Fake.java"), "w", encoding="utf-8") as fh:
            fh.write(FAKE)
        with open(os.path.join(tmp, "Drv.java"), "w", encoding="utf-8") as fh:
            fh.write("package place.poster.app;\npublic class Drv {\n"
                     "  public static void main(String[] a) throws Exception {\n%s\n  }\n}\n" % body)
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        # Stubs FIRST on the source path: the real RunningNote reaches into the signer and
        # stay-connected services, and the stub is what stands in for them.
        c = subprocess.run(["javac", "-nowarn", "-d", out, "-sourcepath",
                            STUBS + os.pathsep + os.path.join(ANDROID, "src", "main", "java")]
                           + SRC + [os.path.join(tmp, "Fake.java"), os.path.join(tmp, "Drv.java")],
                           capture_output=True, text=True, timeout=300)
        assert c.returncode == 0, c.stderr[-4000:]
        r = subprocess.run(["java", "-Dpc.files=" + files, "-cp", out, "place.poster.app.Drv"],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-4000:]
        return r.stdout


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def _code_only(src):
    """Source with its comments removed. Every file here explains itself at length, so a raw-text
    match can be satisfied by the paragraph explaining why the code was removed."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("//"))


# ---- the recording half -----------------------------------------------------------------------

def test_a_crash_is_written_and_can_be_read_back():
    """The whole point: a trace survives the process that produced it."""
    out = run_java("""
      Fake ctx = new Fake();
      CrashLog.install(ctx, "1.0.9999");
      CrashLog.record(ctx, new Thread("pc-sync-handover"),
                      new IllegalStateException("not allowed to start service intent"));
      System.out.println(CrashLog.read(ctx));
    """)
    assert "PosterChan crash" in out
    assert "1.0.9999" in out, "the build number is the first thing a report has to carry"
    assert "pc-sync-handover" in out, "which thread died is half the diagnosis"
    assert "IllegalStateException" in out and "not allowed to start service intent" in out
    assert "at place.poster.app.Drv.main" in out, "the frames themselves must be in the file"


def test_it_records_what_the_phone_was_doing():
    """A trace names a line; it does not say whether the app was on screen or a sweep was in flight,
    and every competing explanation for this crash differs on exactly that."""
    out = run_java("""
      Fake ctx = new Fake();
      CrashLog.install(ctx, "1.0.9999");
      CrashLog.record(ctx, Thread.currentThread(), new RuntimeException("x"));
      System.out.println(CrashLog.read(ctx));
    """)
    assert "app backgrounded" in out or "app on screen" in out
    assert "sync service" in out and "native sweep" in out and "clock fired" in out


def test_a_phone_that_has_never_crashed_reads_empty_not_null():
    out = run_java("""
      Fake ctx = new Fake();
      String s = CrashLog.read(ctx);
      System.out.println("[" + (s == null ? "NULL" : s) + "]");
    """)
    assert "[]" in out, "null would become the string 'null' in the panel"


def test_newest_crash_is_first():
    """The panel shows the top of the file and the crash being asked about is the one that just
    happened."""
    out = run_java("""
      Fake ctx = new Fake();
      CrashLog.install(ctx, "1.0.9999");
      CrashLog.record(ctx, Thread.currentThread(), new RuntimeException("OLDEST"));
      CrashLog.record(ctx, Thread.currentThread(), new RuntimeException("NEWEST"));
      System.out.println(CrashLog.read(ctx));
    """)
    assert out.index("NEWEST") < out.index("OLDEST")


def test_a_crash_loop_cannot_fill_the_disk():
    """A crash that repeats every sixteen minutes must not turn into an unbounded file — but the
    entries that survive have to be the RECENT ones."""
    out = run_java("""
      Fake ctx = new Fake();
      CrashLog.install(ctx, "1.0.9999");
      for (int i = 0; i < 12; i++)
        CrashLog.record(ctx, Thread.currentThread(), new RuntimeException("CRASH-" + i));
      String s = CrashLog.read(ctx);
      int n = 0, at = 0;
      while ((at = s.indexOf("PosterChan crash", at)) >= 0) { n++; at += 5; }
      System.out.println("entries=" + n + " bytes=" + s.length()
                         + " newest=" + s.contains("CRASH-11") + " oldest=" + s.contains("CRASH-0\\n"));
    """)
    assert "entries=3 " in out, "kept entries are bounded"
    assert "newest=true" in out and "oldest=false" in out
    assert int(re.search(r"bytes=(\d+)", out).group(1)) < 24 * 1024


def test_clearing_forgets_it():
    out = run_java("""
      Fake ctx = new Fake();
      CrashLog.install(ctx, "1.0.9999");
      CrashLog.record(ctx, Thread.currentThread(), new RuntimeException("gone"));
      CrashLog.clear(ctx);
      System.out.println("[" + CrashLog.read(ctx) + "]");
    """)
    assert "[]" in out


# ---- the handler half, which is where a crash reporter does its damage -------------------------

def test_the_handler_records_AND_still_lets_the_app_die():
    """NEVER SWALLOW.

    A handler that records and returns leaves the process alive with a dead main looper: a frozen
    app, no dialog, nothing in logcat — strictly worse than the crash it replaced, and the classic
    way a crash reporter makes a bug harder to find. It must delegate to the handler it replaced,
    which on Android is the platform's own (the one that shows the dialog and ends the process).

    Verified by mutation: drop the `prev.uncaughtException(t, e)` line and this fails.
    """
    out = run_java("""
      Fake ctx = new Fake();
      final boolean[] chained = { false };
      Thread.setDefaultUncaughtExceptionHandler(new Thread.UncaughtExceptionHandler() {
        public void uncaughtException(Thread t, Throwable e) { chained[0] = true; }
      });
      CrashLog.install(ctx, "1.0.9999");
      Thread.getDefaultUncaughtExceptionHandler()
            .uncaughtException(Thread.currentThread(), new RuntimeException("BOOM"));
      System.out.println("chained=" + chained[0] + " recorded=" + CrashLog.read(ctx).contains("BOOM"));
    """)
    assert "chained=true" in out, "the platform handler must still run, or the app freezes"
    assert "recorded=true" in out


def test_a_crash_inside_the_handler_does_not_recurse():
    """A handler that throws while handling turns one diagnosable crash into a recursive one. Firing
    the handler from inside the handler must terminate."""
    out = run_java("""
      Fake ctx = new Fake();
      final Thread.UncaughtExceptionHandler[] ours = new Thread.UncaughtExceptionHandler[1];
      Thread.setDefaultUncaughtExceptionHandler(new Thread.UncaughtExceptionHandler() {
        public void uncaughtException(Thread t, Throwable e) {
          if (ours[0] != null) { Thread.UncaughtExceptionHandler h = ours[0]; ours[0] = null;
                                 h.uncaughtException(t, new RuntimeException("SECOND")); }
        }
      });
      CrashLog.install(ctx, "1.0.9999");
      ours[0] = Thread.getDefaultUncaughtExceptionHandler();
      ours[0].uncaughtException(Thread.currentThread(), new RuntimeException("FIRST"));
      System.out.println("survived recorded=" + CrashLog.read(ctx).contains("FIRST"));
    """)
    assert "survived recorded=true" in out


def test_installing_twice_keeps_the_first_chain():
    """Application.onCreate can run more than once in a process's life in some hosts; installing over
    ourselves would chain to ourselves and record every crash twice."""
    out = run_java("""
      Fake ctx = new Fake();
      CrashLog.install(ctx, "1.0.9999");
      Thread.UncaughtExceptionHandler first = Thread.getDefaultUncaughtExceptionHandler();
      CrashLog.install(ctx, "1.0.9999");
      System.out.println("same=" + (first == Thread.getDefaultUncaughtExceptionHandler()));
    """)
    assert "same=true" in out


# ---- the wiring, which is the half that can only be grepped ------------------------------------

def test_the_handler_is_installed_from_the_APPLICATION_not_the_activity():
    """THE CRASH HAPPENS WITH NO ACTIVITY IN EXISTENCE.

    An alarm firing into SyncTickReceiver starts this process cold. A handler installed in
    MainActivity.onCreate covers every path except the one under investigation — and would look
    exactly as correct."""
    app = _code_only(_read(JAVA, "PosterChanApp.java"))
    assert "extends Application" in app
    assert "CrashLog.install(" in app
    manifest = _code_only(_read(ANDROID, "src", "main", "AndroidManifest.xml"))
    assert 'android:name=".PosterChanApp"' in manifest, \
        "an Application class Android never instantiates installs nothing"
    main = _code_only(_read(JAVA, "MainActivity.java"))
    assert "CrashLog.install(" not in main, \
        "installing from the Activity too would chain a second handler on every launch"


def test_the_panel_can_read_it():
    plugin = _code_only(_read(JAVA, "sync", "FolderSyncPlugin.java"))
    assert "public void crashReport(" in plugin and "CrashLog.read(" in plugin
    assert "public void clearCrashReport(" in plugin

    shim = _code_only(_read(ROOT, "static", "js", "client", "fs-android.js"))
    assert "crashReport:" in shim and "clearCrashReport:" in shim

    ui = _code_only(_read(ROOT, "static", "js", "client", "sync.js"))
    assert "crashReport" in ui, "a method nothing calls reports nothing"
    # THREE OUTCOMES, PRINTED DIFFERENTLY: a build too old to answer must not read as an all-clear.
    assert "cannot report crashes" in ui
    assert "no crash recorded" in ui
    assert "LAST CRASH" in ui
