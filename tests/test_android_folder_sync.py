"""Folder sync on Android: the wiring, which is where it would fail silently.

None of this can be driven here — there is no device and the Gradle build runs on CI — so what is
guarded is everything that makes the feature simply ABSENT rather than broken:

  * the plugin is not registered in MainActivity. A plugin that lives in this app is not
    auto-discovered, so `Capacitor.Plugins.FolderSync` is undefined, the shim defines no
    `window.pcFs`, and the Sync screen says "this device can't reach a folder" on a device that can.
  * the JS shim and the Java disagree on the plugin NAME, which fails the same way.
  * a method the shim calls is missing from the plugin — the same silence, one feature at a time.
  * the tree permission is not made PERSISTABLE, so the folder syncs until the next reboot and then
    stops with nothing to say why.
  * DocumentFile creeps back in. It is the obvious API and it is unusable at this scale: a query per
    child plus one per attribute turns 20k photos into tens of thousands of IPCs.
  * the Java exclusion matcher stops being a strict SUBSET of the JavaScript one. That direction
    matters enormously: if Java excluded something JS did not, the scan would omit paths the engine
    still holds in `base`, the engine would read them as "deleted here", and it would delete them
    from every other device. A folder exclusion must never be able to delete anything.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java", "place", "poster", "app")
CLIENT = os.path.join(ROOT, "static", "js", "client")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


def test_the_plugin_is_registered():
    main = _read(JAVA, "MainActivity.java")
    assert "registerPlugin(place.poster.app.sync.FolderSyncPlugin.class)" in main, (
        "FolderSyncPlugin is not registered — Capacitor does not auto-discover a plugin that lives "
        "in this app, so window.Capacitor.Plugins.FolderSync would be undefined and folder sync "
        "would look unavailable on a device that supports it"
    )


def test_the_name_matches_what_the_shim_looks_up():
    plugin = _read(JAVA, "sync", "FolderSyncPlugin.java")
    shim = _read(CLIENT, "fs-android.js")
    m = re.search(r'@CapacitorPlugin\(\s*name\s*=\s*"([^"]+)"', plugin)
    assert m, "the plugin has no @CapacitorPlugin(name=…)"
    assert f"Plugins.{m.group(1)}" in shim, (
        f"the plugin is registered as {m.group(1)} but the shim looks up something else"
    )


def test_every_method_the_shim_calls_exists():
    plugin = _read(JAVA, "sync", "FolderSyncPlugin.java")
    shim = _read(CLIENT, "fs-android.js")
    called = set(re.findall(r"\bP\.(\w+)\(", shim))
    declared = set(re.findall(r"public void (\w+)\(PluginCall", plugin))
    missing = sorted(called - declared)
    assert not missing, f"the shim calls {missing}, which the plugin does not implement"


def test_the_shim_exposes_the_same_surface_as_the_desktop_bridge():
    """One interface, two platforms. A method present on one and missing on the other is a feature
    that works on a laptop and silently does nothing on a phone."""
    shim = _read(CLIENT, "fs-android.js")
    preload = _read(ROOT, "desktop", "preload.js")
    want = ["list", "pick", "forget", "scan", "read", "write", "move", "trash",
            "emptyTrash", "power", "watch", "unwatch", "onChanged"]
    for k in want:
        assert re.search(rf"\b{k}\s*:", shim), f"the Android shim is missing pcFs.{k}"
        assert re.search(rf"\b{k}\s*:", preload), f"the desktop bridge is missing pcFs.{k}"


def test_the_tree_permission_is_persisted():
    plugin = _read(JAVA, "sync", "FolderSyncPlugin.java")
    assert "takePersistableUriPermission" in plugin, (
        "without a persistable grant the folder syncs until the next reboot and then stops, with "
        "nothing in any log to say why"
    )
    assert "FLAG_GRANT_PERSISTABLE_URI_PERMISSION" in plugin


def test_documentfile_is_not_used():
    plugin = _read(JAVA, "sync", "FolderSyncPlugin.java")
    # USE, not the word — the class comment says why not to, and matching that made this test pass
    # on the comment rather than on the code.
    code = re.sub(r"/\*.*?\*/", "", plugin, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    assert "DocumentFile" not in code, (
        "DocumentFile issues a query per child and another per attribute — 20k photos become tens of "
        "thousands of IPCs. The cursor asks for every column for a whole directory in one query."
    )
    assert "buildChildDocumentsUriUsingTree" in code


def test_the_java_matcher_only_handles_literals():
    """It must be a strict SUBSET of foldersync.js's excluder. Anything with a wildcard is left to
    JavaScript, because a Java matcher that excluded MORE than JS would make the scan omit paths the
    engine still has in `base` — which the engine reads as 'deleted here'."""
    ex = _read(JAVA, "sync", "Excludes.java")
    assert 'p.contains("*")' in ex and "continue" in ex, (
        "Excludes.java must skip wildcard patterns rather than interpret them"
    )


def test_the_trash_layout_matches_the_desktop():
    """A folder synced between a phone and a laptop must have ONE trash layout, not two."""
    ex = _read(JAVA, "sync", "Excludes.java")
    bridge = _read(ROOT, "desktop", "fsbridge.js")
    assert '"%04d-%02d-%02d"' in ex and "UTC" in ex
    assert ".pc-trash" in bridge and "TRASH = \".pc-trash\"" in _read(JAVA, "sync", "FolderSyncPlugin.java")


def test_saf_cannot_set_mtime_so_the_write_reports_what_it_became():
    """SAF has no writable last-modified column. If write() returned the mtime it was ASKED for, every
    downloaded file would look locally-edited on the next sweep and be pushed straight back."""
    plugin = _read(JAVA, "sync", "FolderSyncPlugin.java")
    assert "statById" in plugin and 'ret.put("mtime"' in plugin
    assert re.search(r'ret\.put\("mtime",\s*st != null \? st\[1\]', plugin), (
        "write() must report the mtime the provider actually gave the file, not the requested one"
    )
