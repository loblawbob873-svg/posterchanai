"""The folder-sync state machine, RUN rather than grepped.

Everything in this file was previously guarded by matching text in a Java source, and that is how a
day was lost: three bugs shipped through green text-matching tests because every string they looked
for was present and the LOGIC around it was wrong.

  * `nativeEnabled` was a boolean the page computed and pushed. On a cold start the page has no
    drive key yet, so it pushed `false` and overwrote a true value — background sync switched itself
    off on every app launch, and the only symptom was that it worked right after you used the app
    and never otherwise.
  * a per-folder claim had no expiry, so one wedged sweep left the folder permanently "already
    syncing" with no way back but force-stopping the app.
  * the native sweep and the on-screen page raced for the same folder, and the loser was whichever
    one the user was looking at.

None of those is visible in the text. All three are decidable by running the code, which is what
this does: javac against tests/androidstubs, then `java`, with a fake Context whose SharedPreferences
live in a HashMap. No device, no emulator, no Gradle — and no confidence claimed beyond the logic
these classes actually contain.
"""
import os
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android", "app")
JAVA = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app")
STUBS = os.path.join(ROOT, "tests", "androidstubs")

SRC = [os.path.join(JAVA, "sync", f + ".java") for f in
       ("SyncStore", "NativeSweep", "SyncDiff", "Json", "Excludes", "SyncCrypto",
        "NativeRunner", "FolderSyncPlugin", "SyncClock", "SyncCheckWorker", "SafFs", "SyncNet",
        "SyncWork", "SyncService", "SyncTickReceiver")]

# A Context whose preferences are a HashMap, so SyncStore can be driven for real. Written in the
# `place.poster.app.sync` package so the driver can reach package-private members too.
FAKE = """
package place.poster.app.sync;

import android.content.Context;
import android.content.ContentResolver;
import android.content.SharedPreferences;
import java.util.HashMap;
import java.util.Map;

public class Fake extends Context {
  public static final Map<String, Object> STORE = new HashMap<String, Object>();
  public ContentResolver getContentResolver() { return null; }
  /* HOW SLOW THE SYSTEM SERVICES ARE, which on a dozing phone is the whole question. Only the two
   * the sweep policy reads are slowed; the alarm service is not, because re-arming is deliberately
   * done on the looper and slowing it would make every path look equally bad. */
  public static volatile long SLOW_MS = 0;
  public Object getSystemService(String name) {
    if (SLOW_MS > 0 && (Context.CONNECTIVITY_SERVICE.equals(name) || Context.BATTERY_SERVICE.equals(name)))
      try { Thread.sleep(SLOW_MS); } catch (InterruptedException ignored) { }
    return null;
  }
  public java.io.File getFilesDir() { return new java.io.File(System.getProperty("java.io.tmpdir")); }
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
          public boolean commit() { return true; }
        };
      }
    };
  }
}
"""


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def _code_only(src):
    """Source with its comments removed — these files explain themselves at length, and a raw text
    match is otherwise satisfied by the paragraph explaining why the code was removed."""
    import re as _re
    src = _re.sub(r"/\*.*?\*/", "", src, flags=_re.S)
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("//"))


def _need(*tools):
    for t in tools:
        if shutil.which(t) is None:
            pytest.skip("no " + t)


def run_java(body):
    """Compile the real sync sources + a fake Context + a driver, run it, return stdout."""
    _need("javac", "java")
    with tempfile.TemporaryDirectory() as tmp:
        fake = os.path.join(tmp, "Fake.java")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write(FAKE)
        drv = os.path.join(tmp, "Drv.java")
        with open(drv, "w", encoding="utf-8") as fh:
            fh.write("package place.poster.app.sync;\npublic class Drv {\n"
                     "  public static void main(String[] a) throws Exception {\n%s\n  }\n}\n" % body)
        out = os.path.join(tmp, "out")
        os.makedirs(out)
        c = subprocess.run(["javac", "-nowarn", "-d", out, "-sourcepath",
                            STUBS + os.pathsep + os.path.join(ANDROID, "src", "main", "java")]
                           + SRC + [fake, drv], capture_output=True, text=True, timeout=300)
        assert c.returncode == 0, c.stderr[-4000:]
        r = subprocess.run(["java", "-cp", out, "place.poster.app.sync.Drv"],
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-4000:]
        return r.stdout.strip()


ONE_FOLDER = ('[{\\"key\\":\\"Pictures\\",\\"id\\":\\"tree://x\\",\\"enabled\\":true,'
              '\\"paused\\":false}]')


def test_a_startup_push_with_no_drive_key_cannot_switch_background_sync_off():
    """THE ONE THAT COST A DAY.

    `configure()` runs at app startup, before the drive index has necessarily loaded, so the page
    has no `mk` to send. When `enabled` was a stored boolean the page computed, that push wrote
    `false` over a working configuration and the next alarm found the feature off — so folder sync
    ran when you had just used the app and never when you had not, which is indistinguishable from
    the background clock being broken.

    The answer is DERIVED from what is on disk now, and an absent value never erases a stored one.
    """
    out = run_java("""
    Fake ctx = new Fake();
    SyncStore s = new SyncStore(ctx);
    // A complete configuration, the way a sweep pushes it.
    s.configure(true, "https://poster.place", "https://blossom.poster.place", "WRAPPEDKEY",
                "phone", "%s");
    System.out.println("configured=" + s.nativeEnabled());
    // Now the cold-start push: same folders, but the page does not know the key or the servers yet.
    s.configure(false, "", "", "", "phone", "%s");
    System.out.println("afterEmptyPush=" + s.nativeEnabled());
    System.out.println("key=" + (s.wrappedDriveKey().isEmpty() ? "LOST" : "kept"));
    System.out.println("api=" + (s.apiBase().isEmpty() ? "LOST" : "kept"));
    // Signing out is the one thing that means it.
    s.forget();
    System.out.println("afterForget=" + s.nativeEnabled());
""" % (ONE_FOLDER, ONE_FOLDER))
    got = dict(l.split("=", 1) for l in out.splitlines())
    assert got["configured"] == "true"
    assert got["afterEmptyPush"] == "true", (
        "a startup push with no drive key switched background sync off — this is the bug: it comes "
        "back only when you open the app and sweep, which reads as 'it only syncs when I open it'"
    )
    assert got["key"] == "kept" and got["api"] == "kept"
    assert got["afterForget"] == "false", "signing out must actually disable the unattended sweep"


def test_background_sync_is_off_when_it_genuinely_has_nothing():
    """The derivation must still say NO for a device that really cannot sweep — otherwise the alarm
    wakes the phone every sixteen minutes to fail, and `why_off` is what the panel prints."""
    cases = [
        ("no drive key handed over yet", '"", "https://a", "https://b"'),
        ("no server address", '"K", "", "https://b"'),
        ("no media server", '"K", "https://a", ""'),
    ]
    for want, args in cases:
        out = run_java("""
    Fake.STORE.clear();
    SyncStore s = new SyncStore(new Fake());
    String[] v = new String[]{%s};
    s.configure(true, v[1], v[2], v[0], "phone", "%s");
    System.out.println(s.nativeEnabled() + "|" + s.whyDisabled());
""" % (args, ONE_FOLDER))
        on, why = out.split("|", 1)
        assert on == "false", f"expected disabled for {want!r}"
        assert why == want, f"panel would say {why!r}, not {want!r}"
    # …and with no folders at all.
    out = run_java("""
    Fake.STORE.clear();
    SyncStore s = new SyncStore(new Fake());
    s.configure(true, "https://a", "https://b", "K", "phone", "[]");
    System.out.println(s.nativeEnabled() + "|" + s.whyDisabled());
""")
    assert out == "false|no folders paired on this device"


def test_a_wedged_sweep_cannot_brick_a_folder_for_ever():
    """Reported as *"pictures is set to already syncing and stuck / no progress"*.

    The claim is what stops two engines writing one manifest, and nothing holding it can PROMISE to
    give it back: the holder is a thread that can be frozen with the process, killed with the
    renderer, or stuck on a socket. Treated as permanent, one stuck claim means the card says
    "syncing in the background" for ever and the only way out is force-stopping the app.

    So a claim expires. Fresh claims are still refused (the two engines can never actually overlap);
    a stale one is stolen.
    """
    out = run_java("""
    System.out.println("first=" + NativeSweep.claim("Pictures"));
    System.out.println("second=" + NativeSweep.claim("Pictures"));       // fresh holder → refused
    System.out.println("claimedFresh=" + NativeSweep.claimed("Pictures"));
    NativeSweep.release("Pictures");
    System.out.println("afterRelease=" + NativeSweep.claim("Pictures"));
    System.out.println("other=" + NativeSweep.claim("Documents"));       // unrelated folder is free
""")
    got = dict(l.split("=", 1) for l in out.splitlines())
    assert got["first"] == "true"
    assert got["second"] == "false", "two sweeps can hold the same folder — that is a lost manifest"
    assert got["claimedFresh"] == "true"
    assert got["afterRelease"] == "true", "a released claim is not reusable"
    assert got["other"] == "true", "one folder's claim blocks another folder"


def test_the_stale_bound_is_real_and_is_not_a_day():
    """The steal has to be reachable. A bound of hours would be indistinguishable from no bound at
    all for somebody staring at a stuck card, and a bound of seconds would let two engines overlap
    on an ordinary large folder. Read the constant and hold it to a range."""
    src = open(os.path.join(JAVA, "sync", "NativeSweep.java"), encoding="utf-8").read()
    import re
    m = re.search(r"CLAIM_STALE_MS\s*=\s*(\d+)\s*\*\s*60\s*\*\s*1000", src)
    assert m, "the claim has no expiry — a wedged sweep bricks the folder for the life of the process"
    minutes = int(m.group(1))
    assert 10 <= minutes <= 45, (
        f"claim expiry is {minutes} min: under 10 lets a legitimate big-folder sweep be stolen from, "
        f"over 45 is 'stuck for ever' as far as anyone watching the card is concerned"
    )


def test_the_native_sweep_stands_down_while_the_app_is_on_screen():
    """Reported as *"pictures is set to already syncing"* the moment background sync started working.

    Two engines, one folder: the alarm claims it, the person looking at the app presses Sync now, and
    the page is refused with a sentence and no progress of its own. From their side that is a hang
    they caused by opening the app — and the app being open is exactly when the page is the BETTER
    engine, because a visible page is not throttled and can settle what the background sweep defers.

    So visibility decides. This runs `eligible()` against both states with everything else identical.
    """
    out = run_java("""
    Fake.STORE.clear();
    place.poster.app.signer.SignerKey.HAVE = true;
    Fake ctx = new Fake();
    SyncStore s = new SyncStore(ctx);
    s.configure(true, "https://a", "https://b", "K", "phone", "%s");
    FolderSyncPlugin.setForegroundForTest(true);
    System.out.println("appOpen=" + NativeRunner.eligible(ctx) + "|" + NativeRunner.why());
    FolderSyncPlugin.setForegroundForTest(false);
    System.out.println("appHidden=" + NativeRunner.eligible(ctx) + "|" + NativeRunner.why());
""" % ONE_FOLDER)
    got = dict(l.split("=", 1) for l in out.splitlines())
    assert got["appOpen"].startswith("false"), (
        "the alarm sweeps while the app is on screen — that is the claim fight the user sees as "
        "'already syncing' with no progress"
    )
    assert "the app is open" in got["appOpen"], "…and the panel cannot say why it stood down"
    assert got["appHidden"].startswith("true"), (
        "the native sweep does not run when the app is hidden, which is the only case it exists for"
    )


def test_an_unreadable_network_does_not_read_as_offline():
    """THE ONE THAT ACTUALLY STOPPED THE SWEEP, and it was one line.

    `deviceState` set `online = (capabilities != null)`. But
    `getNetworkCapabilities(getActiveNetwork())` returning null is not "offline" — it is "I could not
    tell", and it is exactly what a DOZING device can answer. The alarm exists to fire while the
    device dozes, so the one moment this code was written for was the moment it declared itself
    offline: `shouldSync` answered `offline`, `plan()` found nothing due, and nothing swept. It
    worked whenever the app was open and never with the screen off — the reported symptom, exactly.

    An unreadable network is now UNSET, and `shouldSync` skips a question it has no answer to. A
    manager that IS readable and reports no active network is still a real "offline".
    """
    out = run_java("""
    Fake.STORE.clear();
    place.poster.app.signer.SignerKey.HAVE = true;
    Fake ctx = new Fake();                       // getSystemService → null: the unreadable case
    SyncStore s = new SyncStore(ctx);
    s.configure(true, "https://a", "https://b", "K", "phone", "%s");
    java.util.Map<String,Object> st = NativeRunner.deviceState(ctx);
    System.out.println("onlineKey=" + (st.containsKey("online") ? String.valueOf(st.get("online")) : "unset"));
    FolderSyncPlugin.setForegroundForTest(false);
    System.out.println("eligible=" + NativeRunner.eligible(ctx) + "|" + NativeRunner.why());
""" % ONE_FOLDER)
    got = dict(l.split("=", 1) for l in out.splitlines())
    assert got["onlineKey"] == "unset", (
        "an unreadable network still asserts an answer — if it asserts `false`, every background "
        "tick on a dozing phone declines with 'offline', which is the whole bug"
    )
    assert got["eligible"].startswith("true"), (
        f"the sweep still declines with a network it could not read: {got['eligible']}"
    )


def test_backgrounding_hands_the_folders_over_instead_of_stranding_them():
    """THE REPORT, READ LITERALLY: *"syncing stops shortly after you turn off the screen"*.

    Not "never starts" — a sweep is RUNNING and it dies. The page's sweep is JavaScript, Chromium
    throttles a hidden page's timers to about one a minute, so it does not fail, it STALLS mid-folder
    while still holding the per-folder claim. The next alarm is up to sixteen minutes away, finds the
    folder claimed and skips it, and nothing resumes until the claim expires or the app is opened.
    From outside: it was syncing, you locked the phone, it stopped.

    `onPause` begins a cooperative handover. The page keeps ownership until it has checkpointed its
    journal; its release is the acknowledgement that lets the native sweep take ownership without
    two executors writing the same manifest.
    """
    out = run_java("""
    Fake.STORE.clear();
    place.poster.app.signer.SignerKey.HAVE = true;
    Fake ctx = new Fake();
    SyncStore s = new SyncStore(ctx);
    s.configure(true, "https://a", "https://b", "K", "phone", "%s");

    // The page is sweeping Pictures with the app on screen.
    FolderSyncPlugin.setForegroundForTest(true);
    NativeSweep.claim("Pictures");
    FolderSyncPlugin.notePageClaim("Pictures");
    System.out.println("whileVisible=" + NativeRunner.eligible(ctx));

    // Screen off.
    new FolderSyncPlugin().handleOnPause();
    System.out.println("heldUntilAck=" + NativeSweep.claimed("Pictures"));
    FolderSyncPlugin.releaseForTest("Pictures");
    System.out.println("releasedAfterAck=" + NativeSweep.claimed("Pictures"));
    System.out.println("afterAck=" + NativeRunner.eligible(ctx) + "|" + NativeRunner.why());
""" % ONE_FOLDER)
    got = dict(l.split("=", 1) for l in out.splitlines())
    assert got["whileVisible"] == "false", "the native sweep competes with the page it can see"
    assert got["heldUntilAck"] == "true", (
        "onPause steals the page claim before its durable checkpoint — native and page writers can "
        "then overlap on the same manifest"
    )
    assert got["releasedAfterAck"] == "false"
    assert got["afterAck"].startswith("true"), (
        f"nothing can take over after the page acknowledges its checkpoint: {got['afterAck']}"
    )


def test_a_late_page_release_cannot_free_the_native_sweep_s_claim():
    """The handover makes an old harmless line dangerous.

    `releaseSweep` used to release unconditionally, which was fine while the page was the only thing
    that ever claimed. After `onPause` hands the folders over, a page sweep that was stalled and
    finishes minutes later would free a claim the NATIVE sweep is holding — and two engines writing
    one manifest is last-writer-wins on the document that decides whether files exist.
    """
    out = run_java("""
    NativeSweep.release("Pictures");
    // The page claims, then the screen goes off and asks it to checkpoint.
    NativeSweep.claim("Pictures");
    FolderSyncPlugin.notePageClaim("Pictures");
    new FolderSyncPlugin().handleOnPause();
    System.out.println("heldBeforeAck=" + NativeSweep.claimed("Pictures"));
    FolderSyncPlugin.releaseForTest("Pictures");         // checkpoint acknowledgement
    System.out.println("freedAfterAck=" + NativeSweep.claimed("Pictures"));
    NativeSweep.claim("Pictures");                       // native takes ownership
    System.out.println("nativeHolds=" + NativeSweep.claimed("Pictures"));
    // A duplicate/late release from the old page must not release native ownership.
    FolderSyncPlugin.releaseForTest("Pictures");
    System.out.println("stillHeld=" + NativeSweep.claimed("Pictures"));
""")
    got = dict(l.split("=", 1) for l in out.splitlines())
    assert got["heldBeforeAck"] == "true"
    assert got["freedAfterAck"] == "false"
    assert got["nativeHolds"] == "true"
    assert got["stillHeld"] == "true", (
        "a late release from the page freed the native sweep's claim — a third sweep can now start "
        "on a folder that is already being written"
    )


def test_the_alarm_decides_off_the_main_thread():
    """THE ONE THAT LOOKED LIKE A CRASH FOR A WHOLE DAY, AND WAS NEVER AN EXCEPTION.

    `onReceive` is delivered on the app's MAIN LOOPER, and everything the tick used to do from there
    — a battery read, a connectivity read, a Keystore lookup, a policy pass per folder — is IPC to
    system services. This receiver fires while the device is DOZING, which is when that IPC is at its
    slowest, because dozing is the state the whole feature exists for.

    A blocked main thread is not a crash. Nothing is thrown, so no try/catch sees it, no crash
    handler records it, and no stack trace exists anywhere: the user gets "PosterChan isn't
    responding", and when a receiver overruns its ten seconds the system kills the process outright —
    an app that "just closes, with nothing". Reported as both, one after the other, and four rounds
    of fixes went looking for an exception that was never there.

    So this measures the ONE property that matters: how long the looper is held. The work still has
    to happen — a receiver that returns fast by doing nothing is the same bug wearing a smile — so
    the broadcast must also be FINISHED, which only happens at the end of the real decision.

    Mutation-verified: call `decide(app)` inline in `onReceive` and this fails on the first assert.
    """
    out = run_java("""
    Fake.STORE.clear();
    Fake.SLOW_MS = 0;
    place.poster.app.signer.SignerKey.HAVE = true;
    Fake ctx = new Fake();
    SyncStore s = new SyncStore(ctx);
    s.configure(true, "https://a", "https://b", "K", "phone", "%s");
    FolderSyncPlugin.setForegroundForTest(false);

    Fake.SLOW_MS = 1500;                      // a dozing phone answering a system service
    SyncTickReceiver r = new SyncTickReceiver();
    long t0 = System.currentTimeMillis();
    r.onReceive(ctx, null);
    long held = System.currentTimeMillis() - t0;

    long waited = 0;
    while (!r.pendingResult().finished && waited < 20000) { Thread.sleep(50); waited += 50; }
    System.out.println("looperHeldMs=" + held);
    System.out.println("broadcastFinished=" + r.pendingResult().finished);
    System.out.println("workReallyRan=" + (waited >= 1000));
""" % ONE_FOLDER)
    got = dict(l.split("=", 1) for l in out.splitlines())
    assert int(got["looperHeldMs"]) < 500, (
        "the alarm holds the main thread for the length of a system-service read — on a dozing "
        "phone that is an ANR, which throws nothing, logs nothing and ends with the app simply "
        "closing"
    )
    assert got["broadcastFinished"] == "true", (
        "the broadcast is never finished, so the process can be cached mid-decision — and an "
        "unfinished PendingResult is its own ANR ten seconds later"
    )
    assert got["workReallyRan"] == "true", (
        "the decision did not actually take the slow path, so this test would pass against a "
        "receiver that returns fast by doing nothing at all"
    )


def test_the_service_starts_its_sweep_off_the_main_thread():
    """`onStartCommand` runs on the main looper too, and `NativeRunner.tick` re-runs the whole plan
    — the same Keystore and IPC reads, at the same moment, on the same thread. `startForeground`
    must stay on the looper (the platform kills us for missing its deadline); nothing after it may.

    Wiring, not logic: driving a real Service needs a real Context, so this asserts the shape and
    the RUN test above covers the behaviour."""
    svc = _code_only(_read(JAVA, "sync", "SyncService.java"))
    start = svc[svc.index("public int onStartCommand("):svc.index("private void decide(")]
    assert "startForeground" in start, "the foreground promise must still be kept immediately"
    assert "NativeRunner.tick(" not in start, (
        "the sweep is still decided on the main thread — see the ANR above"
    )
    assert "new Thread(" in start and "decide()" in start
    # The looper used to BE the lock. Off it, two starts can interleave on the flag that decides
    # whether a second sweep runs on a folder already being written.
    assert "synchronized (GATE)" in svc


def test_live_progress_is_only_reported_while_that_folder_is_actually_claimed():
    """The page shows this on a card whose own claim was refused, so a stale line is a lie.

    Reported as "this folder is syncing in the background — it will finish on its own ... what
    bullshit is that! i need to see activity": the numbers existed on the sweep thread and nothing
    published them. Now that they are published, the rules that make them trustworthy are:
    `live()` answers only for a folder something is holding, and a finished sweep clears its own.
    """
    out = run_java(
        'System.out.println("unclaimed=" + (NativeSweep.live() == null));\n'
        '    NativeSweep.claim("Pictures");\n'
        '    NativeSweep.progress("Pictures", "downloading", "DCIM/a.jpg", 41, 6331);\n'
        '    java.util.Map<String,Object> m = NativeSweep.live();\n'
        '    System.out.println("phase=" + m.get("phase") + " done=" + m.get("done")'
        ' + " total=" + m.get("total") + " path=" + m.get("path"));\n'
        '    System.out.println("other=" + (NativeSweep.live().get("key").equals("Pictures")));\n'
        '    NativeSweep.progressDone("Pictures");\n'
        '    System.out.println("cleared=" + (NativeSweep.live() == null));\n'
        '    NativeSweep.progress("Pictures", "uploading", "b.jpg", 1, 2);\n'
        '    NativeSweep.release("Pictures");\n'
        '    System.out.println("released=" + (NativeSweep.live() == null));')
    assert "unclaimed=true" in out, out
    assert "phase=downloading done=41 total=6331 path=DCIM/a.jpg" in out, out
    assert "cleared=true" in out, out
    # …and a claim that goes away takes the line with it, however the sweep ended.
    assert "released=true" in out, out
