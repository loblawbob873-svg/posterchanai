"""No Capacitor plugin method may do slow work on the WebView thread.

THE BUG THIS EXISTS FOR, and it cost a rollback of a whole day's work. Capacitor invokes a
`@PluginMethod` on the WebView's own thread unless the method explicitly hands off with
`getBridge().execute(...)`. That is fine for a method that reads a flag, and fatal for one that talks
to a system service:

  * `SignerPlugin.arm` ends in AndroidKeyStore key generation and an AES-GCM seal — IPC to keystore2
    and work in the secure element, hundreds of milliseconds to seconds on real hardware. It used to
    run only when somebody flipped a switch, on a screen they were watching. Folder sync then began
    arming the same key at EVERY app start, which put it on the UI thread of every launch: "app
    stops responding" seconds after starting, then Android kills the process.
  * `SyncTickReceiver` and `SyncService` had the same shape on the alarm path, where the phone is
    DOZING and system-service IPC is at its slowest.

None of it throws. A blocked main thread produces no exception, no log line and nothing for any
crash handler to catch, so every instrument in this repo is blind to it and the only symptom is an
app that closes.

WHAT THIS CHECKS, AND WHAT IT CANNOT. It is a WIRING test: it reads the source and asserts that any
plugin method whose body reaches known-slow machinery also hands off. It cannot prove the work is
actually slow, and it cannot see a slow call reached indirectly through a helper — for the behaviour
itself see tests/test_android_sync_state.py, which RUNS the alarm receiver against a Context whose
system services sleep and measures how long the calling thread is held.
"""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java", "place", "poster", "app")

# Every plugin the app registers, so a new one joins this check by being written rather than by
# somebody remembering to add it here.
PLUGIN_FILES = []
for dirpath, _dirs, files in os.walk(JAVA):
    for f in files:
        if f.endswith("Plugin.java"):
            PLUGIN_FILES.append(os.path.join(dirpath, f))

# Calls that mean "this is talking to something outside the process".
# NOT bare `getSystemService`: fetching a manager is cheap, and flagging it buried the real ones in
# false positives the first time this was written. These are calls that leave the process and can
# take unbounded time — the secure element, a content provider, or the sync policy which does both.
SLOW = (
    "SignerKey.store", "SignerKey.have", "SignerKey.pubkey", "SignerKey.exposed",
    "sha256Of", "readAll(", "DocumentsContract.",
    "NativeRunner.eligible", "NativeRunner.tick", "NativeRunner.plan",
)

# Methods that are allowed to stay on the WebView thread despite matching, with the reason. Keeping
# the list explicit is the point: an exemption is a decision, not an oversight.
ALLOWED = {
    # Sets a flag read by the alarm's pre-filter. No IPC.
    ("FolderSyncPlugin.java", "setTickPolicy"),
    # In-memory set operations guarding the per-folder claim.
    ("FolderSyncPlugin.java", "claimSweep"),
    ("FolderSyncPlugin.java", "releaseSweep"),
    # Reads counters held in this process.
    ("FolderSyncPlugin.java", "tickStats"),
    # Clears the stored key and stops a service; a user-initiated action on a screen being watched.
    ("SignerPlugin.java", "disable"),
    ("SignerPlugin.java", "enable"),
}

_METHOD = re.compile(r"@PluginMethod[\s\S]{0,400}?public void (\w+)\(PluginCall")


def _strip_comments(src):
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("//"))


def _bodies(path):
    """(name, body) for each @PluginMethod, the body delimited by matching braces.

    Taking "everything up to the next @PluginMethod" is the obvious version and it is wrong for the
    LAST one in a file, whose body then runs to end-of-file and swallows every helper below it — the
    first run of this check reported a method for a call it does not make."""
    src = _strip_comments(open(path, encoding="utf-8").read())
    for m in _METHOD.finditer(src):
        open_at = src.find("{", m.end())
        if open_at < 0:
            continue
        depth, i = 0, open_at
        while i < len(src):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        yield m.group(1), src[open_at:i]


@pytest.mark.parametrize("path", PLUGIN_FILES, ids=[os.path.basename(p) for p in PLUGIN_FILES])
def test_slow_plugin_methods_hand_off_the_webview_thread(path):
    base = os.path.basename(path)
    offenders = []
    for name, body in _bodies(path):
        if (base, name) in ALLOWED:
            continue
        touched = [s for s in SLOW if s in body]
        if not touched:
            continue
        if "getBridge().execute" in body or "new Thread(" in body:
            continue
        offenders.append("%s.%s touches %s on the WebView thread" % (base, name, ", ".join(touched)))
    assert not offenders, (
        "\n".join(offenders)
        + "\n\nCapacitor runs a plugin method on the WebView's thread unless it hands off. Work that "
          "talks to Keystore, a content provider or a system service belongs inside "
          "getBridge().execute(...) — on the startup or alarm path this is an ANR, which throws "
          "nothing, logs nothing, and ends with Android killing the app."
    )


def test_the_check_can_see_a_violation():
    """The guard above is only worth having if it would fire. This is the exact shape that shipped."""
    src = """
      @PluginMethod
      public void arm(PluginCall call) {
        String pub = SignerKey.store(getContext(), sec);
        call.resolve();
      }
    """
    src = _strip_comments(src)
    m = _METHOD.search(src)
    assert m, "the method matcher no longer recognises a plugin method"
    body = src[m.end():]
    assert any(s in body for s in SLOW)
    assert "getBridge().execute" not in body
