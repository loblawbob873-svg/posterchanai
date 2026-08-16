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

    `onPause` is the handover. The page's claims go back — a hidden page cannot finish them, so
    holding them only blocks the engine that can — and the native sweep starts then rather than at
    the next tick.
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
    System.out.println("claimHeld=" + NativeSweep.claimed("Pictures"));
    System.out.println("afterPause=" + NativeRunner.eligible(ctx) + "|" + NativeRunner.why());
""" % ONE_FOLDER)
    got = dict(l.split("=", 1) for l in out.splitlines())
    assert got["whileVisible"] == "false", "the native sweep competes with the page it can see"
    assert got["claimHeld"] == "false", (
        "the page keeps its claim after the screen goes off — a stalled sweep then blocks the only "
        "engine that can still run, which is the whole reported bug"
    )
    assert got["afterPause"].startswith("true"), (
        f"nothing can take over when the app is backgrounded mid-sweep: {got['afterPause']}"
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
    // The page claims, then the screen goes off and the handover moves it to the native sweep.
    NativeSweep.claim("Pictures");
    FolderSyncPlugin.notePageClaim("Pictures");
    new FolderSyncPlugin().handleOnPause();
    System.out.println("freedByHandover=" + NativeSweep.claimed("Pictures"));
    NativeSweep.claim("Pictures");                       // the native sweep takes it
    System.out.println("nativeHolds=" + NativeSweep.claimed("Pictures"));
    // …and now the page's stalled sweep finally reaches its finally block.
    FolderSyncPlugin.releaseForTest("Pictures");
    System.out.println("stillHeld=" + NativeSweep.claimed("Pictures"));
""")
    got = dict(l.split("=", 1) for l in out.splitlines())
    assert got["freedByHandover"] == "false"
    assert got["nativeHolds"] == "true"
    assert got["stillHeld"] == "true", (
        "a late release from the page freed the native sweep's claim — a third sweep can now start "
        "on a folder that is already being written"
    )
