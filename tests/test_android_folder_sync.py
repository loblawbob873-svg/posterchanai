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
    # Capacitor puts these on EVERY plugin proxy itself, so they are not @PluginMethods and never
    # will be. The event they carry is checked properly by test_the_native_tick_is_wired_end_to_end,
    # which matches the emitted name against the subscribed one — exempting them here loses nothing.
    called -= {"addListener", "removeAllListeners", "removeListener"}
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


def test_android_can_verify_a_download_like_the_desktop_does():
    """syncrun skips the checksum check entirely when the adapter has no `hashPart` — a deliberate
    escape hatch for older shells. Without these three the phone and tablet wrote every download
    unverified while the laptop checked every one, and because resume is only permitted where the
    result can be checked, Android also re-downloaded from byte zero after any drop."""
    src = _plugin()
    shim = _read(CLIENT, "fs-android.js")
    for fn in ("hashPart", "discardPart", "partSize"):
        assert "public void %s(PluginCall call)" % fn in src, f"the plugin has no {fn}"
        assert "%s:" % fn in shim, f"the shim does not expose {fn}, so syncrun cannot see it"
    # discardPart must DELETE, never trash: these are bytes we could not confirm, and putting them
    # in the safety net makes the net less trustworthy.
    i = src.index("public void discardPart(PluginCall call)")
    body = src[i: src.index("@PluginMethod", i + 10)]
    assert "deleteDoc(" in body and "trashDoc(" not in body, (
        "an unverified part file is being put in .pc-trash")


def test_the_native_tick_is_wired_end_to_end():
    """THE CLOCK THAT RUNS WITH THE SCREEN OFF, guarded as a chain because it fails silently at every
    link and the failure looks identical each time: nothing syncs, nothing logs, no error anywhere.

    Reported as "syncing stops every time the screen goes off" with "Stay connected" already on.
    Android has no filesystem watcher here (SAF offers none worth having), so the client's only
    automatic trigger was a JS setInterval — and Android throttles timers in a hidden WebView. The
    service was keeping the process alive and nothing was ever asking it to sync.

    Four links, and a break in any one of them is the same silence:
      service ticks -> plugin emits -> shim subscribes -> sync.js nudges.
    """
    svc = _read(JAVA, "push", "StayAwakeService.java")
    plugin = _read(JAVA, "sync", "FolderSyncPlugin.java")
    shim = _read(CLIENT, "fs-android.js")
    sync = _read(CLIENT, "sync.js")

    assert "FolderSyncPlugin.tick(" in svc, (
        "StayAwakeService never ticks — it keeps the process alive and nothing asks it to sync, "
        "which is the screen-off bug exactly"
    )
    assert "setAndAllowWhileIdle" in svc, (
        "the tick is not an alarm that fires in Doze. Handler.postDelayed is scheduled on "
        "uptimeMillis(), which STOPS ADVANCING in deep sleep, and a foreground service keeps the "
        "process resident without keeping the CPU awake — so a Handler fires only when something "
        "else happens to wake the phone, which is exactly the screen-off state this exists for. It "
        "would look like a fix and behave like the bug"
    )
    # USE, not the word. The javadoc above the alarm explains at length why postDelayed is wrong
    # here, so matching the raw file makes this test fail on its own explanation — the same trap
    # test_documentfile_is_not_used already had to step around.
    svc_code = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", svc, flags=re.S))
    assert "postDelayed" not in svc_code, (
        "a Handler is being used to schedule something in this service again. Nothing here may use "
        "one for a delay that has to survive the screen going off — see above"
    )
    assert "ELAPSED_REALTIME_WAKEUP" in svc, (
        "a non-WAKEUP alarm queues until something else wakes the phone, which is the Handler's "
        "failure wearing a different name"
    )
    assert "FLAG_IMMUTABLE" in svc, "Android 12+ throws when a PendingIntent is built without it"
    assert "am.cancel(" in svc, (
        "the alarm is not cancelled in onDestroy — an alarm OUTLIVES the process, so it would "
        "restart the service from a switch the user turned off"
    )

    m_ms = re.search(r"SYNC_TICK_MS\s*=\s*(\d+)\s*\*\s*60", svc)
    assert m_ms, "the tick period is not stated in minutes"
    minutes = int(m_ms.group(1))
    floor = int(re.search(r"minIntervalMs:\s*(\d+)\s*\*\s*60", _read(CLIENT, "foldersync.js")).group(1))
    assert minutes > floor, (
        "the tick period (%d min) is not above the client's minIntervalMs (%d min). Nothing is ever "
        "dirty on Android — there is no watcher — so shouldSync refuses any tick inside that floor, "
        "and a shorter period simply aliases: a 10-minute alarm gives a 20-minute effective period "
        "(sweep at 0, refused at 10, runs at 20)" % (minutes, floor)
    )
    m = re.search(r'notifyListeners\(\s*"([^"]+)"', plugin)
    assert m, "FolderSyncPlugin emits no event, so the tick reaches no WebView"
    event = m.group(1)
    assert event in shim, (
        "the plugin emits %r and the shim listens for something else — the two halves are wired to "
        "different names, which is a tick that is sent and never received" % event
    )
    assert "onTick" in shim, "fs-android.js exposes no onTick for sync.js to subscribe to"
    assert "fs.onTick" in sync, (
        "sync.js never subscribes to the native tick, so the service ticks into nothing"
    )
    assert re.search(r"fs\.onTick\(\s*\(\)\s*=>\s*nudge\([^)]*true", sync), (
        "the tick must nudge with force. Without it nudge() re-checks `_idle()`, which is "
        "document.hidden on a phone with the screen off — so one failed stayConnected read swallows "
        "every tick while the service dutifully keeps sending them"
    )


def test_the_tick_holds_no_key_and_opens_no_socket():
    """The reason background sync could not simply be written in Java: every network step of a sweep
    is signed with the user's nostr key, which with Amber/NIP-46 is not on the device at all. The
    tick sidesteps that by doing none of it — it emits an event and the WebView, which does hold the
    key, performs the sweep. If this ever grows a socket or a key, that decision has been reversed by
    accident."""
    plugin = _read(JAVA, "sync", "FolderSyncPlugin.java")
    body = plugin[plugin.index("public static boolean tick("):]
    body = body[:body.index("\n  }")]
    for forbidden in ("HttpURLConnection", "OkHttp", "Socket", "nsec", "PrivateKey"):
        assert forbidden not in body, "tick() grew a %s — it must only emit" % forbidden


def test_the_background_tick_obeys_the_two_battery_switches():
    """"Only when plugged in" and "Wi-Fi only" must hold for a BACKGROUND sweep, which is the one
    nobody is watching and the one that can quietly spend a data plan.

    Two independent guarantees, and both are needed:
      * the DECISION stays in shouldSync — the tick forces past `_idle()` ("is anyone looking") and
        nothing else, proven against the real sync.js by
        tests/client/test_sync_tick.py::test_the_tick_does_not_bypass_the_policy;
      * the alarm ALSO pre-checks natively, so honouring the switches does not itself cost a
        renderer wake-up every sixteen minutes on a phone in a pocket — which is the battery the
        switches were ticked to save.

    The pre-filter is the dangerous half: it can only ever SUPPRESS. If it could decide, a stale
    policy would silently stop a folder syncing with nothing to say so, so this pins that it reads
    the same two facts shouldSync does and that the client votes with EVERY rather than ANY.
    """
    svc = _read(JAVA, "push", "StayAwakeService.java")
    plugin = _read(JAVA, "sync", "FolderSyncPlugin.java")
    sync = _read(CLIENT, "sync.js")

    assert "suppressed(this)" in svc, (
        "the alarm emits unconditionally, so a phone on battery with 'only when plugged in' set is "
        "woken every 16 minutes to be told no"
    )
    assert "isCharging()" in plugin and "NET_CAPABILITY_NOT_METERED" in plugin, (
        "the native pre-check must read the SAME two facts shouldSync does, or the two halves can "
        "disagree and a folder stops syncing for a reason nothing reports"
    )
    # It may only suppress. A pre-filter that could start a sweep would be a second decision-maker.
    body = plugin[plugin.index("public static boolean suppressed("):]
    body = body[:body.index("\n  }")]
    assert "tick(" not in body and "notifyListeners" not in body, (
        "suppressed() emits — it is a filter, not a trigger"
    )
    # THE WIRE BETWEEN THEM, which is what actually shipped missing. The Java was right and the
    # caller was right; `fs-android.js` had no `setTickPolicy`, so `_pushTickPolicy` returned at its
    # guard, the policy stayed false/false, `suppressed()` returned false at its first statement, and
    # the alarm woke the WebView on cellular exactly as before. Every other assertion in this test
    # passed while the feature was inert — a shape worth naming, because both halves being correct is
    # precisely what makes a missing bridge invisible.
    shim = _read(CLIENT, "fs-android.js")
    for method in ("setTickPolicy", "tickStats"):
        assert re.search(rf"\b{method}\s*:", shim), (
            "fs-android.js does not expose %s, so the native half is unreachable and ships dead"
            % method
        )
        assert "public void %s(PluginCall" % method in plugin, (
            "the shim calls %s but the plugin does not implement it" % method
        )
    assert "_pushTickPolicy" in sync and "startAll" in sync, (
        "nothing ever pushes the policy, so the native pre-filter keeps its false defaults"
    )
    assert "EVERY, not ANY" in sync or "every(" in sync, (
        "the client must vote with EVERY: one folder willing to run on battery has to be able to, "
        "and suppressing on the strictest folder's preference silently stops the others"
    )
    assert "!p.paused" in sync, (
        "a paused folder must not vote — it is not waiting on a charger, it is not started"
    )


def test_the_background_clock_reports_what_the_phone_measured():
    """There is no device in this loop and this failure REPORTS SUCCESS: an alarm that never fires,
    a tick emitted into a dead page and a sweep that ran all look identical from here. The music
    controls cost four APK builds of guessing before they were made to count, so the counters are
    part of the feature rather than a debugging afterthought.

    STATIC, because the case worth explaining is the one where the page was gone — read off an
    instance, the panel would answer 'nothing has ticked this session' about the very tick being
    investigated."""
    plugin = _read(JAVA, "sync", "FolderSyncPlugin.java")
    for counter in ("tArmed", "tFired", "tDelivered", "tDropped", "tSuppressed"):
        assert re.search(r"private static .*\b%s\b" % counter, plugin), (
            "%s is missing or not static — see above" % counter
        )
    assert "public void tickStats(" in plugin, "nothing can read the counters"
    assert "tickStats" in _read(CLIENT, "fs-android.js"), "the bridge does not expose them"
    # AND SOMETHING MUST RENDER THEM. Counters nothing displays are worth exactly as much as no
    # counters — which is what the first version of this shipped, with a javadoc naming a panel that
    # did not exist. On a phone this is the only surface any of it can be read from.
    sync = _read(CLIENT, "sync.js")
    assert "tickStats()" in sync, "nothing in the UI reads the counters"
    assert "sync-bg" in sync, "there is no control that shows them"


def test_emptied_directories_are_removed_but_only_when_empty():
    """"The files are gone but the dirs remain."

    A manifest holds PATHS, never directories, so deleting a folder tombstones the files under it and
    leaves the tree standing on every device. Fixed on both platforms — but the two have OPPOSITE
    safety properties and only one of them is free:

      * desktop uses `rmdir`, which REFUSES a non-empty directory. The syscall is the guard.
      * SAF has no rmdir. `DocumentsContract.deleteDocument` on a directory deletes it RECURSIVELY,
        so here the emptiness check IS the guard, and it must fail CLOSED — a query that returns null
        or throws (provider gone, volume unmounted, grant revoked mid-sweep) has to answer "not
        empty", because being wrong the other way costs somebody's folder.
    """
    plugin = _read(JAVA, "sync", "FolderSyncPlugin.java")
    assert "pruneEmptyDirs(cr, tree, rel)" in plugin, (
        "Android leaves the emptied directory tree behind after a delete"
    )
    assert "private boolean isEmptyDir(" in plugin, "nothing checks the directory is empty first"

    body = plugin[plugin.index("private boolean isEmptyDir("):]
    body = body[:body.index("\n  }")]
    assert "if (c == null) return false;" in body, (
        "a failed query must answer NOT empty. deleteDocument on a directory is recursive, so a "
        "true here deletes a folder whose contents could not be listed"
    )
    assert "catch (Exception e) {\n      return false;" in body or "return false;" in body.split("catch")[-1], (
        "a throw must answer NOT empty, for the same reason"
    )

    # It must run only after the move SUCCEEDED — pruning around a failed trash would remove a
    # directory whose file is still sitting in it.
    trash = plugin[plugin.index("public void trash(PluginCall call)"):]
    trash = trash[:trash.index("@PluginMethod", 10)]
    assert trash.index("could not move") < trash.index("pruneEmptyDirs"), (
        "the prune runs before the failed-trash guard"
    )

    prune = plugin[plugin.index("private void pruneEmptyDirs("):]
    prune = prune[:prune.index("\n  }")]
    assert "TRASH.equals(parts[0])" in prune, "the prune would walk into .pc-trash"
    assert "docId.equals(root)" in prune, (
        "the sync ROOT must never be removed — it is the pairing, and a device that deleted it "
        "would have to re-grant the folder before it could sync again"
    )


def test_a_sweep_holds_a_wake_lock_and_can_never_leak_it():
    """THE MEASURED CAUSE of "syncing stops the moment the screen goes off".

    A foreground service keeps the PROCESS resident — that is what "Stay connected" buys, and it is
    why the WebView survives with the app off screen. It does not keep the PROCESSOR running. On a
    real phone: 23 downloads in the minute before the screen went off, 0 in the minute after, while
    the alarm was firing and the tick was being delivered the whole time. There was no CPU to sweep
    with.

    A wake lock is the one thing here that can flatten a battery, so the three properties that stop
    it leaking are asserted rather than assumed — and the page that holds it is the half Android
    takes away, so every one of them is load-bearing.
    """
    plugin = _read(JAVA, "sync", "FolderSyncPlugin.java")
    sync = _read(CLIENT, "sync.js")
    shim = _read(CLIENT, "fs-android.js")

    assert "PARTIAL_WAKE_LOCK" in plugin, "nothing keeps the CPU awake for a sweep"
    assert "public void sweepBegin(" in plugin and "public void sweepEnd(" in plugin
    assert "wakeBegin" in shim and "wakeEnd" in shim, "the bridge does not expose it, so it ships dead"

    body = plugin[plugin.index("public void sweepBegin("):]
    body = body[:body.index("\n  }")]
    assert "if (!wake.isHeld())" not in body, (
        "the lock is taken only when not already held, so it is never RENEWED — a timed wake lock is "
        "not extended by being held, so a sweep longer than the bound loses the CPU part-way and "
        "stops mid-file. Reported as 'seems to last longer', which was exactly the timeout"
    )
    assert "acquire(WAKE_MAX_MS)" in body, (
        "an untimed acquire is held for ever if the renderer is killed mid-sweep — which is the "
        "exact case this exists for"
    )
    assert "setReferenceCounted(false)" in plugin, (
        "begin/end cross a bridge that can drop either; a counted lock left at +1 by one lost end "
        "is never released again"
    )
    assert "releaseWake();" in plugin[plugin.index("protected void handleOnDestroy()"):][:400], (
        "the page going away must drop the lock — nothing else can, since the only thing that would "
        "have released it was the sweep running in that page"
    )

    # Taken for a real sweep only, and given back on every exit including a throw.
    assert "!o.dryRun && _wake && _wake.wakeBegin" in sync, "a dry run must not hold the CPU up"
    tail = sync[sync.index("running.delete(f.id);"):][:400]
    assert "wakeEnd" in tail, "a sweep that threw would keep the processor awake"


def test_a_long_sweep_renews_the_cpu_lease():
    """A TIMED wake lock is not renewed by being held — the OS reclaims it when the bound expires. So
    taking it once at the start of a sweep buys exactly WAKE_MAX_MS and then the device suspends
    mid-file, which is the original bug arriving ten minutes later.

    The bound must STAY (a renderer killed mid-sweep cannot be allowed to hold the processor all
    night), so the sweep renews it while there is still work — from `step`, the one call every loop
    already makes per file, throttled because it crosses the Capacitor bridge."""
    run = _read(CLIENT, "syncrun.js")
    assert "_keepAwake" in run, "nothing renews the wake lock during a long sweep"
    body = run[run.index("const _keepAwake ="):]
    body = body[:body.index("\n    };")]
    assert "wakeBegin" in body, "the renewal does not reach the platform"
    assert "60000" in body or "throttl" in body.lower(), (
        "an unthrottled renewal crosses the bridge for every file in the folder"
    )
    assert "typeof fs.wakeBegin !== 'function'" in body, (
        "desktop and older APKs have no wakeBegin; this must be a no-op there, not a throw"
    )
    # It has to be wired into the per-file call, or it renews nothing.
    assert "_keepAwake();" in run[run.index("const step = (phase"):][:200], (
        "step() does not renew, so the lease expires part-way through a long sweep"
    )
