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


def _code_only(src):
    """Source with its comments removed.

    Every file here explains itself at length, so a test that matches raw text can be satisfied by
    the very paragraph explaining why the code it is looking for was removed. That is not
    hypothetical — it happened twice while these were being written."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("//"))


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
    assert ".pc-trash" in bridge and "TRASH = \".pc-trash\"" in _saf()


def test_saf_cannot_set_mtime_so_the_write_reports_what_it_became():
    """SAF has no writable last-modified column. If write() returned the mtime it was ASKED for, every
    downloaded file would look locally-edited on the next sweep and be pushed straight back."""
    saf = _saf()
    assert "statById" in saf
    for fn in ("public long[] write(", "public long[] commitPart("):
        body = saf[saf.index(fn):]
        body = body[:body.index("\n    }")]
        assert re.search(r'st != null \? st\[1\]', body), (
            fn + " must report the mtime the provider actually gave the file, not the requested one"
        )
    # …and the plugin must hand that straight back rather than echoing what it was asked for.
    assert 'ret.put("mtime", st[1])' in _plugin()


# ---- failures must FAIL, not resolve --------------------------------------------------------
#
# Three findings from the folder-sync review, all the same shape: an operation that did not happen,
# reported as one that did. On Android that is worse than on desktop, because SAF has no
# rename-over-an-existing-document and no writable mtime, so the executor has to believe what the
# plugin tells it about the filesystem.

def _plugin():
    return _read(JAVA, "sync", "FolderSyncPlugin.java")


def _saf():
    """The SAF filesystem itself.

    It used to live inside FolderSyncPlugin, reachable only as a @PluginMethod — i.e. only from a
    page calling across the Capacitor bridge, which is exactly the half Android takes away when the
    screen goes off. It is SafFs.java now so the native background sweep can call the same code, and
    every rule below is asserted against wherever it actually lives rather than against a file
    name."""
    return _read(JAVA, "sync", "SafFs.java")


def test_a_file_is_never_unlinked_when_the_trash_is_unavailable():
    """THE WORST ONE. trashDoc used to call deleteDoc() when the .pc-trash directory could not be
    created, and return a path implying the file had been trashed — so the caller recorded a
    successful delete for a file that no longer exists anywhere.

    It breaks the single guarantee this feature makes, and it fires exactly when things are already
    going wrong: a partially revoked grant, an unmounted volume, a FILE named .pc-trash shadowing the
    directory. Every one of those is temporary. The deletion is not."""
    src = _saf()
    i = src.index("public String trashDoc(")
    body = src[i: src.index("\n    public ", i + 10)]
    head = body[: body.index("String name = baseName(rel);")]
    assert "if (destDir == null) return null;" in head, (
        "trashDoc no longer refuses when the trash is unavailable")
    assert "deleteDoc(" not in head, (
        "trashDoc unlinks the user's file when it cannot trash it — that is data loss, not a fallback")


def test_a_failed_trash_rejects_instead_of_resolving():
    """A null from trashDoc used to resolve as `{to: null}`. syncrun then pushed report.trashed and
    agreed a TOMBSTONE for a file still on disk — so the next sweep read it as a local edit and
    re-uploaded it, resurrecting a file deleted on another device, reporting "1 to trash" both
    times."""
    saf = _saf()
    body = saf[saf.index("public String trash(String rel, long when)"):]
    body = body[:body.index("\n    }")]
    assert 'if (dest == null) throw' in body, (
        "a failed trash still answers a path — the sweep will record a tombstone for a file that is "
        "still there, and re-upload it on the next pass")
    # …and the plugin must not turn that throw back into a resolve.
    plug = _plugin()
    pbody = plug[plug.index("public void trash(PluginCall call)"):]
    pbody = pbody[:pbody.index("@PluginMethod", 10)]
    assert "call.reject(" in pbody


def test_write_commit_checks_that_the_rename_actually_happened():
    """SAF cannot rename over an existing document, so the old file is trashed FIRST. If that move
    fails the name is still taken, the rename fails too, and childId/statById answer with the OLD
    file's size and mtime — which resolved as success. `base` then claimed the remote version was
    present, and since it matched the manifest by csum and the disk by size+mtime, the update was
    never retried: the newer version silently never landed on that device."""
    src = _saf()
    body = src[src.index("public long[] commitPart(String rel, long when)"):]
    body = body[:body.index("\n    }")]
    assert "trashDoc(existing, rel, when) == null" in body, (
        "commitPart does not check that the previous file was cleared")
    assert body.count("throw new java.io.IOException(") >= 4, (
        "commitPart still has paths that report success without checking: "
        + str(body.count("throw new java.io.IOException(")))
    assert 'if (finalId == null) throw' in body, (
        "commitPart answers a stat even when the committed file cannot be found")


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
    saf = _saf()
    shim = _read(CLIENT, "fs-android.js")
    for fn in ("hashPart", "discardPart", "partSize"):
        assert "public void %s(PluginCall call)" % fn in src, f"the plugin has no {fn}"
        assert "%s:" % fn in shim, f"the shim does not expose {fn}, so syncrun cannot see it"
    # discardPart must DELETE, never trash: these are bytes we could not confirm, and putting them
    # in the safety net makes the net less trustworthy.
    body = saf[saf.index("public void discardPart(String rel)"):]
    body = body[:body.index("\n    }")]
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
      alarm fires -> receiver ticks -> plugin emits -> shim subscribes -> sync.js nudges.

    The clock has MOVED out of StayAwakeService since this was written — see
    test_the_clock_does_not_depend_on_an_unrelated_switch for why that was the whole bug — so this
    reads it wherever it lives now.
    """
    svc = _read(JAVA, "sync", "SyncClock.java") + _read(JAVA, "sync", "SyncTickReceiver.java")
    plugin = _read(JAVA, "sync", "FolderSyncPlugin.java")
    shim = _read(CLIENT, "fs-android.js")
    sync = _read(CLIENT, "sync.js")

    assert "FolderSyncPlugin.tickOnMain(" in svc, (
        "the alarm never ticks — something keeps the process alive and nothing asks it to sync, "
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
        "there is no way to cancel the alarm — an alarm OUTLIVES the process, so an account that "
        "stopped syncing would go on waking the phone every sixteen minutes for ever"
    )
    # …and it is re-armed by the delivery itself. setAndAllowWhileIdle is one-shot, so a receiver
    # that does not re-arm ticks exactly once per app start and then never again — which reads as
    # "background sync works for a bit and then stops", i.e. as the bug.
    recv = _code_only(_read(JAVA, "sync", "SyncTickReceiver.java"))
    body = recv[recv.index("public void onReceive("):]
    assert body.index("SyncClock.arm(") < body.index("FolderSyncPlugin.tickOnMain("), (
        "the alarm is re-armed after work that can throw — one throw and the clock is gone for the "
        "life of the install, silently"
    )

    m_ms = re.search(r"PERIOD_MS\s*=\s*(\d+)\s*\*\s*60", svc)
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
    svc = _read(JAVA, "sync", "SyncTickReceiver.java")
    plugin = _read(JAVA, "sync", "FolderSyncPlugin.java")
    sync = _read(CLIENT, "sync.js")

    assert "suppressed(ctx)" in svc, (
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
    clock = _read(JAVA, "sync", "SyncClock.java")
    for counter in ("tDelivered", "tDropped", "tSuppressed"):
        assert re.search(r"private static .*\b%s\b" % counter, plugin), (
            "%s is missing or not static — see above" % counter
        )
    # The ALARM's counters live with the alarm — one clock, one place that knows about it.
    for counter in ("cArmed", "cFired", "cForeground", "cRefused"):
        assert re.search(r"private static .*\b%s\b" % counter, clock), (
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
    saf = _saf()
    assert "pruneEmptyDirs(rel)" in saf, (
        "Android leaves the emptied directory tree behind after a delete"
    )
    assert "public boolean isEmptyDir(" in saf, "nothing checks the directory is empty first"

    body = saf[saf.index("public boolean isEmptyDir("):]
    body = body[:body.index("\n    }")]
    assert "if (c == null) return false;" in body, (
        "a failed query must answer NOT empty. deleteDocument on a directory is recursive, so a "
        "true here deletes a folder whose contents could not be listed"
    )
    assert re.search(r"catch \(Exception e\) \{\s*return false;", body), (
        "a throw must answer NOT empty, for the same reason"
    )

    # It must run only after the move SUCCEEDED — pruning around a failed trash would remove a
    # directory whose file is still sitting in it.
    trash = saf[saf.index("public String trash(String rel, long when)"):]
    trash = trash[:trash.index("\n    }")]
    assert trash.index("could not move") < trash.index("pruneEmptyDirs"), (
        "the prune runs before the failed-trash guard"
    )

    prune = saf[saf.index("public void pruneEmptyDirs("):]
    prune = prune[:prune.index("\n    }")]
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
    # The `finally` of the sweep itself, not the first `running.delete` in the file — there are five
    # now (an early return when the folder has no bridge, the native hand-off, the queue drain), and
    # anchoring on the first one pointed this assertion at a branch that never took the lock. It
    # silently stopped measuring the release path, which is the only thing it was written to check.
    fin = sync.rindex("} finally {")
    tail = sync[fin:][:700]
    assert "running.delete(f.id);" in tail, "the sweep's finally block has moved — re-anchor this"
    assert "wakeEnd" in tail, "a sweep that threw would keep the processor awake"


def test_a_long_sweep_renews_the_cpu_lease():
    """A TIMED wake lock is not renewed by being held — the OS reclaims it when the bound expires. So
    taking it once at the start of a sweep buys exactly WAKE_MAX_MS and then the device suspends
    mid-file, which is the original bug arriving ten minutes later.

    The bound must STAY (a renderer killed mid-sweep cannot be allowed to hold the processor all
    night), so the sweep renews it while there is still work — from `step`, the one call every loop
    already makes per file, throttled because it crosses the Capacitor bridge."""
    run = _read(CLIENT, "syncexec.js")
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


def test_a_sweep_makes_sure_javascript_is_actually_running():
    """A WAKE LOCK KEEPS THE CPU UP AND DOES NOTHING ABOUT THE WEBVIEW.

    `WebView.pauseTimers()` is APP-WIDE and stops all JavaScript in the process — every timer, every
    scheduled task — and the activity lifecycle can call it when the app is backgrounded. A sweep
    with no JavaScript running is a sweep that does not run, however awake the processor is and
    however faithfully the alarm fires. Reported for hours as "background syncing stops after a short
    period" and starts again the moment the app is opened, which is that shape exactly.

    `resumeTimers()` is idempotent, so where nothing paused them this costs nothing; and it is taken
    WITH the wake lock, scoped to a sweep, rather than being a standing keep-awake flag."""
    plugin = _read(JAVA, "sync", "FolderSyncPlugin.java")
    body = plugin[plugin.index("public void sweepBegin("):]
    body = body[:body.index("\n  }")]
    assert "resumeTimers" in body, (
        "a sweep acquires the CPU but never checks that JavaScript is running — pauseTimers() is "
        "app-wide and stops the sweep dead while the lock is happily held"
    )
    assert "runOnUiThread" in body, "WebView calls must be made on the UI thread"
    # Scoped to a sweep, not a standing flag: it must not appear in the service. Comments stripped —
    # that file now EXPLAINS why the clock left it, and a test that fails on its own explanation is
    # the trap test_documentfile_is_not_used already had to step around.
    svc = _read(JAVA, "push", "StayAwakeService.java")
    svc = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", svc, flags=re.S))
    assert "resumeTimers" not in svc, (
        "keeping timers running for the life of the service is a standing keep-awake, which is the "
        "battery cost the whole policy exists to avoid"
    )


# ---- THE SWEEP THAT RUNS WITHOUT THE WEBVIEW ----------------------------------------------------
#
# The tick above asks the page to sweep, and Chromium throttles a hidden page's JavaScript however
# awake the processor is — so on a backgrounded phone that request often reaches nobody. The transfer
# therefore also exists in Java. Every link below is one whose absence is total silence: no sync, no
# error, and a counter panel that still says the clock is ticking perfectly.

def test_the_native_sweep_is_wired_from_the_alarm_to_the_engine():
    svc = _code_only(_read(JAVA, "sync", "SyncTickReceiver.java"))
    runner = _code_only(_read(JAVA, "sync", "NativeRunner.java"))
    plugin = _plugin()
    shim = _read(CLIENT, "fs-android.js")
    sync = _read(CLIENT, "sync.js")

    assert "NativeRunner.tick(" in svc or "SyncService.start(" in svc, (
        "the alarm never reaches the native sweep — every tick still goes to a WebView that may be "
        "throttled, which is the bug this exists to fix"
    )
    # …and the JS tick still happens, unconditionally. An account signed in through Amber has no key
    # here, and a folder holding one conflict is deferred by the native sweep on every run — a phone
    # that stopped ticking the page for either reason would sync nowhere at all. See
    # test_the_native_tick_never_silences_the_webview_one.
    assert "FolderSyncPlugin.tickOnMain(" in svc, (
        "the native path swallows the tick — an Amber account then has neither engine"
    )
    assert "public static boolean eligible(" in runner, (
        "nothing can ask whether a sweep would run without starting one, so the only way to find "
        "out is to start a foreground service and put an item in somebody's shade to discover that "
        "no folder was due — every sixteen minutes, for ever"
    )

    for fn in ("configure", "forgetNative", "nativeReport", "claimSweep", "releaseSweep"):
        assert "public void %s(PluginCall call)" % fn in plugin, "the plugin has no " + fn
    for fn in ("configureNative", "forgetNative", "nativeReport", "claimSweep", "releaseSweep"):
        assert "%s:" % fn in shim, "the shim does not expose %s, so it ships dead" % fn
    assert "_pushNativeConfig" in sync, "nothing ever tells the phone where the servers are"
    assert "PC.driveKeyWrapped" in sync, "the phone is never given a key, so it can decrypt nothing"


def test_the_key_the_phone_is_given_is_the_wrapped_one():
    """No new secret at rest is the whole reason a native sweep is acceptable. What is handed over is
    the NIP-44 self-wrapped value the drive index already publishes; the phone opens it with the
    account secret its own signer holds."""
    app = _read(CLIENT, "app.js")
    assert "driveKeyWrapped: () => (FilesIdx && FilesIdx._mkWrapped)" in app, (
        "the accessor hands over something other than the wrapped key"
    )
    # …and it must NOT be _ensureMK, which MINTS a key when the account has none — from a config push
    # that happens on every sweep, including before a pull has answered. That would write a key which
    # decrypts nothing over the real one.
    i = app.index("driveKeyWrapped:")
    assert "_ensureMK" not in app[i:i + 200]
    store = _read(JAVA, "sync", "SyncStore.java")
    assert 'if (wrappedKey != null && !wrappedKey.isEmpty()) e.putString(K_MK, wrappedKey);' in store, (
        "an empty key erases the stored one — a configure() that arrives before the signer answers "
        "would silently turn background sync off until somebody opened the app again"
    )
    assert "public void forget()" in store and "forgetNative" in _read(CLIENT, "app.js"), (
        "signing out leaves the wrapped key on a handed-down phone"
    )


def test_only_one_engine_may_sweep_a_folder():
    """Two sweeps writing the same manifest is last-writer-wins on the document that decides whether
    files exist, and the moment it is most likely is somebody opening the app while the alarm is
    mid-sweep. The lock has to be NATIVE, because the page's own `running` map cannot see Java."""
    sweep = _read(JAVA, "sync", "NativeSweep.java")
    sync = _read(CLIENT, "sync.js")
    assert "public static synchronized boolean claim(" in sweep
    assert "if (!claim(f.key))" in sweep, "the native sweep does not take its own lock"
    assert "claimSweep(keyOf(f))" in sync, "the page sweeps without asking whether Java is in there"
    assert "releaseSweep(keyOf(f))" in sync, (
        "a claim that is never released is a folder the background sweep can never touch again"
    )
    # Released in the finally, not on the happy path.
    fin = sync[sync.index("running.delete(f.id);"):]
    fin = fin[:fin.index("})();")]
    assert "releaseSweep" in fin


def test_the_unattended_sweep_refuses_what_it_cannot_ask_about():
    """A background sweep has nobody in front of it, so the standing rule is that it fails closed —
    and refusing suppresses ONE BUCKET, never the sweep. A guard that aborts everything is the same
    bug with the sign flipped, which is exactly what happened to the contacts sweep: "it deleted
    everything" became "it syncs nothing, for ever"."""
    sweep = _read(JAVA, "sync", "NativeSweep.java")
    body = sweep[sweep.index("List<Map<String, Object>> verdicts = SyncReconcile.check("):]
    body = body[:body.index("Journal j = new Journal(")]
    assert "SyncReconcile.apply(planned, verdicts)" in body and "return" not in body, (
        "a refused mass delete aborts the whole sweep instead of dropping the deletions"
    )
    # The refusals are RECORDED, or the next foreground sweep has no idea there is something to ask.
    for kind in ("refusedTrash", "refusedResurrect", "refusedRemoteDelete"):
        assert kind in body, "a background sweep no longer reports that it refused: " + kind
    # An empty agreement FORCES A HASH; it must not stop the sweep. The first version deferred
    # instead, and since `base` here is written only by this sweep and the page keeps its own copy
    # where Java cannot read it, the whole native path could never run once on any device — every
    # alarm answered "first sync — open the app once" and opening the app did nothing.
    # COVERAGE, not emptiness — an interrupted first sweep leaves a handful of entries behind, and
    # "empty" then never fires again: the conflict storm's back door.
    assert "boolean thin = index.isEmpty()" in sweep
    assert "if (thin) { hash = true;" in sweep
    assert 'rep.deferredWhy = "first sync' not in sweep, (
        "the native sweep defers on an empty agreement again — nothing else ever writes one, so it "
        "can never run"
    )
    # …and a DEFERRAL still advances the clock, while an ERROR does not. Requiring `deferred == 0`
    # reads as caution and is a battery leak: a conflict is deferred by design and may never be
    # settled until somebody opens the app, so that folder would be swept on every single alarm, for
    # ever, to defer it again. An error is different — nothing was learned — so the next tick retries.
    runner = _read(JAVA, "sync", "NativeRunner.java")
    assert "if (rep.error.isEmpty()) {" in runner
    assert "rep.deferred == 0" not in runner


def test_the_native_sweep_holds_the_processor_and_gives_it_back():
    """A foreground service keeps the PROCESS resident and does nothing about the PROCESSOR —
    measured as 23 downloads in the minute before the screen went off and 0 in the minute after. A
    timed lock is not renewed by being held, and renewing on progress is what left a long download
    without one, so the renewal is a clock."""
    runner = _read(JAVA, "sync", "NativeRunner.java")
    assert "PARTIAL_WAKE_LOCK" in runner
    assert "acquire(WAKE_MAX_MS)" in runner, "an untimed lock survives a crash mid-sweep"
    assert "setReferenceCounted(false)" in runner
    assert runner.count("acquire(WAKE_MAX_MS)") >= 2, "the lock is taken once and never renewed"
    fin = runner[runner.index("} finally {"):]
    assert "wake.release()" in fin and "renew.cancel()" in fin, "the lock or its timer can leak"
    assert "java.util.Arrays.fill(sec, (byte) 0)" in fin, "the account key is left in memory"


def test_the_panel_can_say_why_a_background_sweep_did_nothing():
    """There is no device in this loop and this failure REPORTS SUCCESS: an account whose key is not
    here, a folder deferred, a manifest the server refused to shrink — every one of them is a phone
    whose alarm counters look perfect and which syncs nothing."""
    sync = _read(CLIENT, "sync.js")
    assert "nativeReport()" in sync, "nothing in the UI reads what the native sweep did"
    assert "no key on this device" in sync, "the commonest reason is not spelled out anywhere"


def test_the_phone_is_given_an_absolute_server_and_not_its_own_bundle():
    """`location.origin` in the packaged app is `https://localhost` — the app's OWN bundle. Every
    fetch in the page is relative and that is right; the native sweep is not in the page, so it needs
    a URL it can open a socket to, and the wrong one is not an error: the phone POSTs a manifest into
    itself every sixteen minutes and reports a failure nobody reads.

    `''` is the standalone case (no instance at all) and must turn the whole thing OFF rather than be
    treated as a base."""
    sync = _read(CLIENT, "sync.js")
    app = _read(CLIENT, "app.js")
    body = sync[sync.index("async function _pushNativeConfig()"):]
    body = body[:body.index("\n  }")]
    assert "PC.serverOrigin" in body, "the native config is built from the page's own origin"
    assert "apiBase: location.origin" not in body
    # The same four facts, now named once and reused: `wanted` gates BOTH the `enabled` flag and
    # whether the account key is sealed into the keystore at all (see test_folder_sync_arms_the_
    # native_key_itself — arming on a device that syncs nothing would be a cost with no feature).
    assert "const wanted = !!mk && !!api && !!media && list.length > 0;" in body, (
        "a missing server or media host still reports the sweep as enabled"
    )
    assert "enabled: wanted," in body, "the flag no longer comes from that decision"
    assert "serverOrigin: _serverOrigin," in app, "PC does not expose the instance base"
    # …and _serverOrigin must keep answering '' with no instance, or the guard above never fires.
    fn = app[app.index("function _serverOrigin()"):]
    fn = fn[:fn.index("\n")]
    assert "if(_standalone()) return ''" in fn


def test_the_native_tick_never_silences_the_webview_one():
    """`NativeRunner.tick` can only answer "a thread was spawned", never "the work happened" — a
    folder holding one conflict is deferred on every single run. Skipping the WebView tick on the
    strength of it silenced the engine that COULD have settled that folder, and because the skip is
    process-wide, every other folder on the phone with it."""
    svc = _code_only(_read(JAVA, "sync", "SyncTickReceiver.java"))
    # `decide()`, not `onReceive`: the deciding moved OFF the main thread, because a Keystore lookup
    # and a battery read on the looper of a dozing phone is an ANR — no exception, no log, and the
    # app "just closes". The rule below is unchanged; only which method holds it.
    body = svc[svc.index("static void decide("):]
    assert "FolderSyncPlugin.tickOnMain(" in body, "the WebView is never asked"
    # The page tick must come out of the receiver UNCONDITIONALLY — before anything that inspects the
    # native path, so no later branch can be written that skips it.
    assert body.index("FolderSyncPlugin.tickOnMain(") < body.index("NativeRunner.eligible("), (
        "the WebView tick sits after (and can therefore be made conditional on) the native path — "
        "see above"
    )


def test_a_claim_does_not_outlive_the_page_that_took_it():
    """The claim is released in the sweep's `finally`, which does not run when the renderer is killed
    mid-sweep — the case this whole path exists for. Left held, NEITHER engine can touch that folder
    again for the life of the process: the native sweep answers "already syncing" and the reloaded
    page is told "syncing in the background", on every press."""
    plugin = _plugin()
    sweep = _read(JAVA, "sync", "NativeSweep.java")
    assert "public static synchronized void releaseAll(" in sweep
    body = plugin[plugin.index("protected void handleOnDestroy()"):]
    body = body[:body.index("\n  }")]
    assert "NativeSweep.releaseAll(" in body, "a page that dies mid-sweep strands the folder for ever"
    # Only the PAGE's claims: a native sweep running in the service must survive the page dying,
    # which is the entire point of it.
    assert "pageClaims" in body and "BUSY" not in body


def test_the_clock_does_not_depend_on_an_unrelated_switch():
    """THE BUG THIS FILE SPENT FOUR ROUNDS NOT FINDING, because every link it did guard was fine.

    The alarm lived inside StayAwakeService — the "Stay connected" foreground service, which is OFF
    BY DEFAULT and is a NOTIFICATIONS feature (a fallback for receiving DMs and calls where no push
    distributor is installed). So on a phone that had never touched that switch there was no clock at
    all: the Doze alarm, the wake lock, its renewal, resumeTimers and finally an entire native sweep
    engine were all downstream of a tick nothing ever emitted. Folder sync worked while the screen was
    on, because the page's own heartbeat ran, and stopped when it went off — reported exactly that
    way, on a phone and a tablet, across several rounds of "fixed".

    Three properties, and the first one is the fix:
      * the clock is armed by FOLDER SYNC, from its own configure() and from boot;
      * StayAwakeService no longer arms one, or a phone that does have that switch on gets two
        alarms and two sweeps;
      * it is cancelled when the account syncs nothing, or an alarm wakes the phone every sixteen
        minutes for ever to decide there is no work.
    """
    plugin = _code_only(_read(JAVA, "sync", "FolderSyncPlugin.java"))
    boot = _code_only(_read(JAVA, "push", "BootReceiver.java"))
    stay = _read(JAVA, "push", "StayAwakeService.java")

    assert "SyncClock.followStore(" in plugin, (
        "configure() does not arm the clock, so background sync depends on something else having "
        "armed it — which is the bug"
    )
    assert "SyncClock.followStore(" in boot, (
        "an alarm does not survive a reboot. Without this line background sync stops at the next "
        "restart and stays stopped until somebody opens the app — a bug that shows up days later"
    )
    assert "SyncClock.cancel(" in plugin, "nothing ever stops the clock"

    # StayAwakeService may CANCEL the alarm it used to own (an update inherits the old PendingIntent)
    # but must not arm one. Comments are stripped: this file explains at length why the clock left.
    stay_code = _code_only(stay)
    assert "setAndAllowWhileIdle" not in stay_code, (
        "StayAwakeService arms a folder-sync alarm again. There is one clock; a second here doubles "
        "every wake-up on exactly the phones that opted into the switch"
    )
    assert "NativeRunner.tick(" not in stay_code, (
        "StayAwakeService sweeps again — the sweep belongs to the clock that every phone has, not "
        "to the one only some phones turned on"
    )
    # …and the legacy alarm is still cleared, or an install upgrading from that build keeps firing a
    # tick into a service that no longer handles it.
    assert "cancelLegacyTick" in stay_code, (
        "an alarm armed by the previous build outlives the update and nothing cancels it"
    )


def test_the_background_sweep_gets_to_hold_the_process():
    """A RECEIVER GETS TEN SECONDS AND THEN THE PROCESS IS CACHED — and a cached process on Android
    12+ is FROZEN. Threads stop. Sockets stall. Nothing throws and nothing logs, and the sweep resumes
    only if something else brings the app back. That is "not running long in the background, shortly
    after you turn the screen off" precisely, and no amount of wake lock fixes it: a wake lock is the
    CPU, and this is the process.

    So the sweep runs inside a foreground service, which is what every sync app on Android does. The
    refusal path matters as much: Android 12+ throws on a background foreground-service start outside
    its exemptions, and a phone that refuses must still sweep (worse, and not nothing) AND SAY SO —
    otherwise the fallback is indistinguishable from the fix working.
    """
    recv = _code_only(_read(JAVA, "sync", "SyncTickReceiver.java"))
    svc = _code_only(_read(JAVA, "sync", "SyncService.java"))
    manifest = _read(
        os.path.join(ROOT, "mobile", "android", "app", "src", "main", "AndroidManifest.xml"))

    assert "SyncService.start(" in recv, "the sweep never gets a foreground service"
    assert re.search(r'android:name="\.sync\.SyncService"', manifest), (
        "SyncService is not declared — an undeclared service simply does not start"
    )
    assert re.search(r'android:name="\.sync\.SyncTickReceiver"', manifest), (
        "the receiver is not declared, so the alarm lands nowhere at all"
    )
    assert re.search(r'\.sync\.SyncService"[\s\S]{0,300}foregroundServiceType="specialUse"', manifest), (
        "SyncService declares no foreground service type — or declares dataSync, which Android 15 "
        "caps at six hours a day across the whole app: a first sync of a real Pictures folder is "
        "hours of transfer, so that type silently stops background sync for the rest of the day on "
        "exactly the folders that need it (tests/test_android_call_service.py bans it outright)"
    )
    assert "FOREGROUND_SERVICE_SPECIAL_USE" in manifest, (
        "Android 14+ refuses to START a service whose declared type it has no permission for, so "
        "without this the sweep silently falls back to the bare thread this exists to replace"
    )
    assert re.search(r'\.sync\.SyncService"[\s\S]{0,600}PROPERTY_SPECIAL_USE_FGS_SUBTYPE', manifest), (
        "Android 14+ requires the subtype property beside a specialUse service"
    )
    # The fallback, and the counter that tells the two apart on a phone nobody here can hold.
    assert "onForegroundRefused()" in recv and "NativeRunner.tick(" in recv, (
        "a refused foreground service either crashes or silently syncs nothing — it must fall back "
        "AND be counted"
    )
    # The fallback must ALSO hold the process, or it is a fallback to the original bug.
    assert "SyncWork.start(" in recv, (
        "the refusal path drops straight to a bare thread, which is the pre-fix behaviour: the "
        "process is cached seconds later and frozen mid-sweep. An expedited job carries no "
        "background-start restriction and keeps the process out of the freezer"
    )
    assert recv.index("SyncWork.start(") < recv.index("NativeRunner.tick("), (
        "the bare thread is tried before the job — it must be the last resort, not the first"
    )
    # …and the refusal can arrive at the SERVICE instead of at the call site, where giving up would
    # be a total silent failure sitting under a panel reporting a successful foreground start.
    assert "SyncWork.start(" in svc and "onForegroundRefused()" in svc, (
        "SyncService's own startForeground failure neither counts the refusal nor falls back, so "
        "nothing syncs and the counters say it did"
    )
    assert "STOP_FOREGROUND_DETACH" in svc, (
        "the sweep finishing removes the shared notification out from under the signer or "
        "'stay connected' if either is up"
    )
    assert "onTimeout" in svc, (
        "a foreground-service time limit arrives as onTimeout and then a kill — a kill mid-transfer "
        "is the one thing a sweep handles worse than not running"
    )
    assert "START_NOT_STICKY" in svc and "START_STICKY" not in svc, (
        "a sticky relaunch restarts this service with a null intent and no sweep behind it"
    )


def test_the_alarm_is_exact_where_the_platform_allows_it():
    """This looks like a question about punctuality and is the one that decides whether a background
    sweep can hold the process at all.

    Android 12+ refuses a background foreground-service start outside a short list of exemptions, and
    the one this path relies on is "your app invokes an EXACT alarm". An inexact
    `setAndAllowWhileIdle` is temp-allowlisted with foreground services explicitly NOT allowed — so
    with the inexact form alone, the service start is refused on essentially every tick and the sweep
    falls back for the whole life of the install. Sixteen minutes either way is the same clock; the
    exemption is the whole difference.

    Asked for, never demanded: it is granted by default on 12 and is a user permission on 13+, which
    is exactly why the job route exists beside it rather than as a nicety."""
    clock = _code_only(_read(JAVA, "sync", "SyncClock.java"))
    assert "setExactAndAllowWhileIdle" in clock, "the clock never asks for an exact alarm"
    assert "canScheduleExactAlarms" in clock, (
        "an exact alarm is scheduled without checking whether this phone allows one — on Android 13+ "
        "that throws SecurityException, and the throw is inside the arm, so it would end the clock"
    )
    assert "setAndAllowWhileIdle" in clock, (
        "there is no inexact fallback, so a phone that has not granted the permission gets no clock "
        "at all — a strictly worse outcome than a clock whose sweep runs as a job"
    )
    # And the panel has to be able to say which one this phone got, since the two behave identically
    # right up to the moment the service start is refused.
    assert "isExact(" in clock and "clockExact" in _read(JAVA, "sync", "FolderSyncPlugin.java"), (
        "nothing reports whether the alarm is exact, so 'every sweep ran as a job' has no explanation"
    )


def test_a_sweep_already_running_is_not_swept_again():
    """`plan()` is asked BEFORE a foreground service is started, so it has to know what the other
    engine is holding. With the app open the page claims every folder first; without this the alarm
    still answered "eligible", started a service, had every claim refused, and amounted to a
    notification appearing and vanishing — the exact cost `plan()` was split out of `tick()` to
    avoid."""
    runner = _code_only(_read(JAVA, "sync", "NativeRunner.java"))
    sweep = _read(JAVA, "sync", "NativeSweep.java")
    assert "public static synchronized boolean claimed(" in sweep, (
        "there is no way to ASK whether a folder is claimed without taking the claim"
    )
    assert "NativeSweep.claimed(" in runner, (
        "the plan counts folders another engine is already sweeping as due"
    )


def test_folder_sync_arms_the_native_key_itself():
    """THE SECOND HALF OF THE SAME BUG, and the same shape as the background signer's.

    The native sweep signs every network step, so it refuses to start without a Keystore-sealed
    secret — and the only two things that ever stored one were the "Sign for other apps on this
    phone" switch and pairing a laptop over NIP-46. Two unrelated features, in two other parts of
    settings, that somebody syncing a folder has no reason to have touched. So an ordinary account
    got "the account key is not on this device" about a key the page was holding, and the native
    sweep — the entire point of writing one — never ran once.

    A feature asks for what it needs, itself.
    """
    sync = _read(CLIENT, "sync.js")
    app = _read(CLIENT, "app.js")
    runner = _read(JAVA, "sync", "NativeRunner.java")

    assert "armNativeSigner" in app, "nothing exposes the arming call to the rest of the client"
    body = sync[sync.index("async function _pushNativeConfig("):]
    body = body[:body.index("\n  }")]
    # COMMENTS STRIPPED, and this is not fussiness: the first version of this test passed with the
    # call deleted, because the explanation left behind still named it. A guard that its own
    # rationale satisfies guards nothing.
    body = _code_only(body)
    assert "armNativeSigner" in body, (
        "folder sync hands the phone its settings without ever handing it the key those settings "
        "are useless without"
    )
    # …and it happens BEFORE the configure, or the first background tick after a fresh sign-in is
    # refused for a reason the next one cannot fix any faster.
    assert body.index("armNativeSigner") < body.index("fs.configureNative("), (
        "the key is armed after the settings are pushed"
    )
    # LOCAL KEYS ONLY. With Amber or a bunker there is nothing on this device to hand over, and
    # arming with nothing would produce a sweep that fails every upload instead of declining.
    arm = app[app.index("async _armNative()"):]
    arm = arm[:arm.index("\n    },")]
    assert "ME.mode !== 'local'" in arm, (
        "the arming path does not check that this account HAS a local key here"
    )
    assert "SignerKey.have(" in runner, (
        "the sweep no longer checks for a key at all — it would try to sign with nothing"
    )


def test_the_key_is_only_armed_for_a_device_that_syncs():
    """`_pushNativeConfig` runs at startup on EVERY Android launch. Arming there unconditionally
    would seal the nsec into the keystore of every local-key user on the platform, including everyone
    who has never opened Folder Sync — a real cost (it is what lets an unattended process sign as
    you) paid by people getting no feature for it. It is gated on the same four facts that decide
    whether the native sweep is enabled at all."""
    sync = _code_only(_read(CLIENT, "sync.js"))
    body = sync[sync.index("async function _pushNativeConfig("):]
    body = body[:body.index("\n  }")]
    assert "if(haveFolders){" in body and "armNativeSigner" in body, (
        "the account key is sealed into the keystore whether or not this device syncs anything"
    )
    # GATED ON FOLDERS, NOT ON THE FULL `wanted`. `wanted` includes the drive key, which is the value
    # most likely to be absent on a cold start — so gating the arming on it meant the one push that
    # could have armed the key was the one push with nothing to arm with. The native side no longer
    # lets an empty push switch anything off, so arming early and configuring fully a moment later
    # converges rather than fighting.
    assert body.index("const haveFolders =") < body.index("armNativeSigner"), (
        "the gate is computed after the arming it is supposed to gate"
    )


def test_the_armed_key_is_this_account_s_key():
    """"Already holds one" has to mean "holds THIS account's".

    Switching accounts reloads the page with a new session and does not clear the keystore, so a
    phone that armed account A kept A's secret. That was nearly inert while only the NIP-46 signer
    used it; the background sweep DEPENDS on it now, so B's unattended sweep would sign its Blossom
    auth and its manifest proof as A and try to unwrap B's drive key with A's secret — a 403 at best,
    the wrong identity at worst, and silence on both paths."""
    app = _code_only(_read(CLIENT, "app.js"))
    arm = app[app.index("async _armNative()"):]
    arm = arm[:arm.index("\n    },")]
    assert "st.pubkey" in arm and "ME.pubkey" in arm, (
        "the arming path treats any stored key as this account's — see above"
    )


def test_signing_out_takes_the_account_key_off_the_phone():
    """The wrapped drive key used to be safe on its own because the secret that opens it lived in
    `Session`, which logout clears. Folder sync now seals that secret into the Android keystore so an
    unattended sweep can sign — so without this, "log out" leaves the previous account's nsec on a
    handed-down phone, usable by the background signer and by the sweep."""
    app = _code_only(_read(CLIENT, "app.js"))
    body = app[app.index("function logout(){"):]
    body = body[:body.index("_forgetPhonebook()")]
    assert "forgetNative" in body, "the wrapped drive key outlives the session"
    assert re.search(r"_capPlugin\('Signer',\s*'disable'\)", body), (
        "the account SECRET stays in the keystore after signing out"
    )


def test_the_two_engines_fold_case_the_same_way():
    """`Pattern.CASE_INSENSITIVE` alone folds ASCII; JavaScript's `i` folds Unicode. A folder typed
    as `Übungen` and spelled `übungen` on disk is then excluded by the browser and not by the phone,
    and the two sync different sets from one exclusion list."""
    diff = _read(JAVA, "sync", "SyncDiff.java")
    assert "Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE" in diff


def test_the_background_sweeps_progress_reaches_the_card():
    """Four links, and the chain is dead if any one of them is missing.

    Android only builds in CI, so the wiring is grepped and the logic is run elsewhere
    (tests/test_android_sync_state.py drives NativeSweep.progress/live under java).

    The page refuses to sweep a folder the native engine holds — correctly — and used to print one
    static sentence, which on a folder of any size is indistinguishable from a hang.
    """
    java = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                        "place", "poster", "app", "sync")
    sweep = _read(java, "NativeSweep.java")
    plugin = _read(java, "FolderSyncPlugin.java")
    adapter = _read(CLIENT, "fs-android.js")
    page = _read(CLIENT, "sync.js")

    assert 'progress(f.key, "downloading"' in sweep, "the native sweep no longer reports downloads"
    assert 'progress(f.key, "uploading"' in sweep, "the native sweep no longer reports uploads"
    assert "progressDone(f.key)" in sweep, "a finished sweep leaves its last line on the card for ever"
    assert "public void nativeLive(" in plugin, "the plugin no longer exposes the live progress"
    assert "nativeLive: () => P.nativeLive()" in adapter, "the adapter no longer asks for it"
    assert "_watchNative(f" in page, "a refused claim no longer watches the sweep that won it"
    # A build whose plugin cannot answer keeps the old sentence — the only honest thing to print.
    assert "typeof fs.nativeLive !== 'function'" in page, \
        "an older APK would now show a blank status instead of the fallback sentence"


def test_an_inflight_page_sweep_survives_the_android_handoff():
    """A manual multi-GB download is already authorized work, not a new scheduled sweep.

    Reapplying onlyWhenCharging when Android hides/recreates the WebView abandoned the transfer and
    changed its card from a live percentage to "waiting until you plug in". The native continuation
    must be recorded before the page claim is released, bypass scheduler policy once, and still
    honor a later explicit Pause.
    """
    java = os.path.join(JAVA, "sync")
    plugin = _code_only(_read(java, "FolderSyncPlugin.java"))
    runner = _code_only(_read(java, "NativeRunner.java"))
    mark = plugin.index("NativeRunner.continueFolders(continuing)")
    release = plugin.index("NativeSweep.releaseAll(")
    assert mark < release, "the page claim is released before its continuation is recorded"
    assert "if (continuing(f.key) && f.enabled && !f.paused)" in runner, (
        "the native handoff either reapplies charging policy or ignores an explicit Pause"
    )
    tick = runner[runner.index("public static boolean tick(Context ctx, String why, final Runnable done)"):]
    assert tick.index("consumeContinuations(p.due)") < tick.index("running = true"), (
        "a handed-over manual sweep remains permanently exempt from scheduling policy"
    )


def test_a_fresh_native_sync_cannot_publish_missing_files_as_deletions():
    """The native engine needs the same durable first-sync boundary as the page engine.

    Journal coverage is not proof that a baseline completed: an interrupted phone may have more
    than half its records before Android storage is cleared or remounted. Until one whole sweep
    ends cleanly, absences must fetch live records and never create remote tombstones.
    """
    store = _read(JAVA, "sync", "SyncStore.java")
    sweep = _read(JAVA, "sync", "NativeSweep.java")
    assert "baselineComplete(" in store and "markBaselineComplete(" in store
    assert "final boolean joining = !store.baselineComplete(f.key);" in sweep
    assert "planned.tombstone.clear()" in sweep
    assert "store.markBaselineComplete(f.key)" in sweep
    assert sweep.index("planned.tombstone.clear()") < sweep.index("for (Map<String, Object> t : plan.tombstone)"), (
        "a joining phone can still reach the tombstone publisher"
    )
    assert "stop == null || !stop.stopping()" in sweep, (
        "an interrupted native sweep can certify an incomplete first-sync baseline"
    )
