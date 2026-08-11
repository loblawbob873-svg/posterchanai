"""Folder sync that never started, and could not say why.

Run: venv-unified/bin/python -m pytest tests/test_folder_sync_startup.py

Reported as "on android phone, why is Documents in Folder Sync 'not syncing yet' not started yet? it
was working before" — and "it was working before" is the tell. This is a STARTUP RACE: fine most
times, dead some times, and an app update is enough to change which.

Three faults, each of which alone produces exactly that screen:

  1. `fs-android.js` read `Capacitor.Plugins.FolderSync` ONCE, at script-evaluation time, and gave up
     if it was not there. That map is EMPTY for a plugin registered in Java with no JS package of its
     own (`registerPlugin(name)` resolves those), and it may not be populated at all before the page's
     scripts run. Either way `window.pcFs` is never set, for the whole session.
  2. `startAll()` returns early when there is no adapter — correct — and nothing ever called it
     again, so an adapter that installed a moment later was never noticed.
  3. Every automatic sweep was `sweep(f).catch(()=>{})`. A folder whose sweep THROWS — no filesystem
     plugin, a SAF permission the OS dropped after an update, a manifest the server refused — keeps
     whatever status it had, and for a folder that has never swept that is the placeholder "not
     synced yet". The error existed the whole time; nothing showed it.

The first two are why it stopped. The third is why nobody could tell.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FSA = (ROOT / "static" / "js" / "client" / "fs-android.js").read_text(encoding="utf-8")
SYNC = (ROOT / "static" / "js" / "client" / "sync.js").read_text(encoding="utf-8")


def test_the_adapter_finds_a_java_only_plugin():
    """`Capacitor.Plugins.<name>` is empty for those; registerPlugin is what resolves them."""
    assert "cap.registerPlugin('FolderSync')" in FSA, (
        "the adapter can only find the plugin if it happens to be in Capacitor.Plugins")


def test_it_only_registers_on_a_native_platform():
    """In a browser registerPlugin hands back a proxy that accepts every call and rejects it —
    replacing "this device cannot sync" with "every sync operation fails", which is worse and wrong."""
    i = FSA.index("function _plugin()")
    body = FSA[i:i + 700]
    assert "isNativePlatform" in body
    assert body.index("isNativePlatform") < body.index("registerPlugin")


def test_a_bridge_that_arrives_late_is_still_picked_up():
    """The native bridge can inject its plugin list after this script runs. Deciding once meant
    deciding for the session."""
    assert "if(!install())" in FSA and "setInterval" in FSA, (
        "the adapter gives up permanently when the bridge is not ready yet")
    assert "n > 40" in FSA, "the retry is unbounded"


def test_the_desktop_adapter_is_never_replaced():
    """The desktop shell sets window.pcFs itself, and it is not this file's."""
    i = FSA.index("function install()")
    assert "if(window.pcFs) return true;" in FSA[i:i + 300]


def test_start_all_retries_when_there_is_no_adapter_yet():
    i = SYNC.index("function startAll(){")
    body = SYNC[i:i + 900]
    assert "startAll._t" in body, "a missing adapter stops folder sync for the whole session"
    assert "if(FS()) startAll();" in body


def test_opening_the_screen_starts_it_too():
    """The moment somebody is looking at it and expecting it to work."""
    i = SYNC.index("function paint(){")
    assert "startAll()" in SYNC[i:i + 700]


def test_a_thrown_sweep_reaches_the_card():
    """A decline is not an error and already shows its reason. This is only for the throw, which was
    swallowed by every automatic caller."""
    assert "function swept(f, opts)" in SYNC
    assert "sweep(f, {}).catch(()=>{})" not in SYNC, "an automatic sweep still swallows its error"
    i = SYNC.index("function swept(f, opts)")
    body = SYNC[i:i + 600]
    assert "setStatus(f.id" in body, "the error is caught and then dropped"
