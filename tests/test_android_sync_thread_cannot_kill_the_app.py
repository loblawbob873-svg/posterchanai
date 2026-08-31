"""A background sync thread may never let an exception escape, because that ends the process.

Reported repeatedly: Folder Sync on Android "tries to load, then returns you to desktop", and after
a few attempts "android says: there is a bug and it closed". Both sentences describe the same thing
— the app PROCESS dying — and on a phone where PosterChan is the launcher, a dead process lands you
on the home screen.

`NativeRunner` starts the sweep on a thread whose body was:

    try { sweepAll(app, p.due, p.deep); }
    finally { if (done != null) { try { done.run(); } catch (Throwable ignored) { } } }

try/FINALLY, with no catch. On Android an exception leaving a Runnable is not a logged error — the
default handler kills the process. Nothing reaches our own reporting, because the reporting lives
inside the method that threw.

It looked safe because sweepAll catches Throwable itself. Two ways past that, both real:

  * `SyncStore store = new SyncStore(ctx)` ran BEFORE the try, so a failure opening the store had
    nothing to catch it at all;
  * the catch block ALLOCATED — a LinkedHashMap and a JSON string — to record the failure. An
    OutOfMemoryError raised inside a catch that then allocates is thrown again, out of the catch,
    past the finally, and out of the thread. Syncing a folder full of files on a phone is precisely
    where OOM happens, which is why this reproduced on a real phone and never on a desktop.

These are source assertions on purpose. The condition is "an exception escapes a thread", which
cannot be observed from inside the process it kills, and the emulator suite cannot seed the folder
that produces it. What CAN be checked, exactly, is that no path out of that Runnable is unguarded.
"""
import re
import unittest
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "mobile" / "android" / "app" / "src" / "main"
       / "java" / "place" / "poster" / "app" / "sync" / "NativeRunner.java").read_text(
    encoding="utf-8")


def _strip_comments(java):
    """Comments are prose ABOUT the code and must not be searched as if they were code.

    The first version of this file looked for the word "catch" in the Runnable and found it in the
    comment explaining why a catch was needed — so it measured the documentation, reported the guard
    as missing, and would have gone on doing that however the code was written.
    """
    java = re.sub(r"/\*.*?\*/", "", java, flags=re.S)
    return re.sub(r"//[^\n]*", "", java)


def _runnable_body():
    start = SRC.index('Thread t = new Thread(new Runnable()')
    return _strip_comments(SRC[start:SRC.index('"pc-native-sync"', start)])


def _sweep_all():
    start = SRC.index("private static void sweepAll")
    return SRC[start:SRC.index("\n    }\n", start)]


def _sweep_all_code():
    return _strip_comments(_sweep_all())


class TheSweepThreadIsSealed(unittest.TestCase):
    def test_the_runnable_catches_throwable(self):
        body = _runnable_body()
        self.assertRegex(body, r"catch\s*\(\s*Throwable",
                         "the sweep Runnable has no catch, so anything sweepAll lets out kills the "
                         "app process — which is what 'returns you to desktop' means")

    def test_the_catch_comes_before_the_finally(self):
        """`finally` runs on the way out and does not stop the throw; only a catch does."""
        body = _runnable_body()
        self.assertLess(body.index("catch"), body.index("finally"),
                        "the guard is a finally, not a catch: %s" % body[-200:])

    def test_the_completion_callback_is_still_run_when_the_sweep_dies(self):
        """Whoever asked for the sweep is waiting; swallowing the error must not also swallow the
        notification that it finished, or the card sits on 'syncing' for ever."""
        self.assertIn("done.run()", _runnable_body())


class TheReporterCannotBeTheKiller(unittest.TestCase):
    def test_the_store_is_opened_inside_the_guarded_region(self):
        body = _sweep_all_code()
        head = body[:body.index("try {")]
        self.assertNotIn("new SyncStore(ctx)", head,
                         "the store is opened before the try, so a failure opening it has nothing "
                         "to catch it and reaches the thread")
        self.assertIn("store = new SyncStore(ctx);", body[body.index("try {"):])

    def test_reporting_a_failure_cannot_throw_its_own(self):
        body = _sweep_all_code()
        catch = body[body.index("} catch (Throwable t) {"):body.index("} finally {")]
        self.assertIn("new LinkedHashMap", catch, "this test is pointed at the wrong block")
        alloc = catch.index("new LinkedHashMap")
        guard = min((catch.index(t) for t in ("try {",) if t in catch), default=len(catch))
        self.assertLess(guard, alloc,
                        "the failure recorder allocates outside a try. Under OutOfMemoryError — the "
                        "commonest way a phone sync dies — that allocation throws again, out of the "
                        "catch, and takes the process with it")

    def test_a_missing_store_does_not_dereference_null(self):
        catch = _sweep_all_code()
        self.assertIn("if (store != null)", catch,
                      "the catch calls store.setLastReport() unconditionally, but the store is now "
                      "opened inside the try and is null exactly when opening it failed")


if __name__ == "__main__":
    unittest.main()
