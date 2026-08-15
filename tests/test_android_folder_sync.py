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


# ---- failures must FAIL, not resolve --------------------------------------------------------
#
# Three findings from the folder-sync review, all the same shape: an operation that did not happen,
# reported as one that did. On Android that is worse than on desktop, because SAF has no
# rename-over-an-existing-document and no writable mtime, so the executor has to believe what the
# plugin tells it about the filesystem.

def _plugin():
    return _read(JAVA, "sync", "FolderSyncPlugin.java")


def test_a_file_is_never_unlinked_when_the_trash_is_unavailable():
    """THE WORST ONE. trashDoc used to call deleteDoc() when the .pc-trash directory could not be
    created, and return a path implying the file had been trashed — so the caller recorded a
    successful delete for a file that no longer exists anywhere.

    It breaks the single guarantee this feature makes, and it fires exactly when things are already
    going wrong: a partially revoked grant, an unmounted volume, a FILE named .pc-trash shadowing the
    directory. Every one of those is temporary. The deletion is not."""
    src = _plugin()
    i = src.index("private String trashDoc(")
    body = src[i: src.index("\n  private ", i + 10)]
    head = body[: body.index("String name = baseName(rel);")]
    assert "if (destDir == null) return null;" in head, (
        "trashDoc no longer refuses when the trash is unavailable")
    assert "deleteDoc(cr, tree, docId)" not in head, (
        "trashDoc unlinks the user's file when it cannot trash it — that is data loss, not a fallback")


def test_a_failed_trash_rejects_instead_of_resolving():
    """A null from trashDoc used to resolve as `{to: null}`. syncrun then pushed report.trashed and
    agreed a TOMBSTONE for a file still on disk — so the next sweep read it as a local edit and
    re-uploaded it, resurrecting a file deleted on another device, reporting "1 to trash" both
    times."""
    src = _plugin()
    i = src.index("public void trash(PluginCall call)")
    body = src[i: src.index("@PluginMethod", i + 10)]
    assert 'if (dest == null) { call.reject(' in body, (
        "a failed trash still resolves — the sweep will record a tombstone for a file that is there")


def test_write_commit_checks_that_the_rename_actually_happened():
    """SAF cannot rename over an existing document, so the old file is trashed FIRST. If that move
    fails the name is still taken, the rename fails too, and childId/statById answer with the OLD
    file's size and mtime — which resolved as success. `base` then claimed the remote version was
    present, and since it matched the manifest by csum and the disk by size+mtime, the update was
    never retried: the newer version silently never landed on that device."""
    src = _plugin()
    i = src.index("public void writeCommit(PluginCall call)")
    body = src[i: src.index("@PluginMethod", i + 10)]
    assert "trashDoc(cr, tree, existing, rel, call.getLong(\"when\", 0L)) == null" in body, (
        "writeCommit does not check that the previous file was cleared")
    assert body.count("call.reject(") >= 3, (
        "writeCommit still has paths that report success without checking: " + str(body.count("call.reject(")))
    assert "if (finalId == null) { call.reject(" in body, (
        "writeCommit resolves even when the committed file cannot be found")


def test_empty_trash_can_actually_empty_it():
    """`getInt("days", 30)` cannot tell an explicit 0 from an absent value — the same blind spot that
    made the desktop's Empty trash unable to remove anything newer than a month."""
    src = _plugin()
    i = src.index("public void emptyTrash(PluginCall call)")
    body = src[i: src.index("@PluginMethod", i + 10)]
    assert "call.getInt(\"days\")" in body and "daysArg == null ? 30 : daysArg" in body, (
        "an explicit days:0 still falls back to the 30-day window")
    assert "if (days > 0) {" in body, (
        "with days:0 the folder NAME is still consulted — a future-dated or unparseable trash "
        "directory then survives for ever, which is a permanent leak in the one place a user goes "
        "to reclaim space")
