#!/usr/bin/env python3
"""A device run in which the APP CRASHED is a failed run, even when every test passed.

Found 2026-10-24 (run 40128423159 attempt 2): all 168 instrumented tests passed while gradle printed

    Logcat of last crash:
    Process: place.poster.app, PID: 5317
    java.lang.RuntimeException: java.lang.reflect.InvocationTargetException
    ...
    Caused by: java.lang.NullPointerException ... MusicService.notification

-- a real crash of the app process (a cross-thread publish meeting onDestroy), which takes the media session the car
talks to with it. The run would have gone green with it in the log, because a crash that happens BETWEEN assertions,
or in a test that then recovers, fails nothing. No device test crashes the app on purpose (a force-stop or a kill logs
no FATAL EXCEPTION), so any Java crash of OUR process during the run is a bug.

Reads gradle's own log (`Logcat of last crash:`) and logcat's crash buffer (`FATAL EXCEPTION` ... `Process: <pkg>`).
Another app's crash (Play Services, the launcher, a WebView renderer's native death) is not ours and is not counted.
Usage: android_crash_verdict.py <file>... ; exit 1 and print the stack when the app crashed, else 0.
"""
import re
import sys
from pathlib import Path

PACKAGE = "place.poster.app"
# `Process: place.poster.app, PID: 5317` -- exactly our process, never `place.poster.app:remote` style children
# being matched as something else, and never a package that merely starts with ours.
PROCESS = re.compile(r"Process: (%s)(?::[\w.]+)?, PID: \d+" % re.escape(PACKAGE))
STRIP = re.compile(r"^.*?\b[EF] AndroidRuntime:\s?")      # logcat threadtime prefix, when present


def crashes(text):
    """Every crash of our process in `text`, each as its stack (first lines)."""
    lines = [STRIP.sub("", l) for l in text.splitlines()]
    found = []
    for i, line in enumerate(lines):
        m = PROCESS.search(line)
        if not m:
            continue
        # A crash report: gradle's "Logcat of last crash:" or logcat's "FATAL EXCEPTION" just above it.
        head = " ".join(lines[max(0, i - 3):i])
        if "FATAL EXCEPTION" not in head and "Logcat of last crash" not in head:
            continue
        stack = [lines[i]]
        for nxt in lines[i + 1:i + 40]:
            if not nxt.strip() or PROCESS.search(nxt) or nxt.startswith(("BUILD ", "> Task", "FAILURE")):
                break
            stack.append(nxt)
        found.append("\n".join(stack))
    # The same crash appears in both sources; one report each.
    return list(dict.fromkeys(found))


def main(paths):
    seen = []
    for p in paths:
        try:
            seen += crashes(Path(p).read_text(errors="replace"))
        except OSError:
            continue
    seen = list(dict.fromkeys(seen))
    if not seen:
        print("no crash of %s during the device run" % PACKAGE)
        return 0
    first = seen[0].splitlines()
    cause = next((l for l in reversed(first) if l.startswith("Caused by:")), first[1] if len(first) > 1 else first[0])
    print("::error title=The app crashed during the device run::%s -- %s" % (PACKAGE, cause.strip()[:300]))
    for s in seen:
        print(s)
        print("----")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
