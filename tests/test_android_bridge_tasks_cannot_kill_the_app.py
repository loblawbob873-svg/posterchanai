"""A task handed to the Capacitor bridge runs AFTER Capacitor's try/catch, so a throw ends the app.

"Folder Sync just crashes the app and returns you to desktop." "If you launch it a few times,
android says: there is a bug and it closed." "Folder sync still not even opening on android! then I
get the prompt to clear the cache for PosterChan!"

Capacitor invokes a @PluginMethod inside its own try/catch and turns a throw into a rejected call —
so an exception raised SYNCHRONOUSLY inside a plugin method is handled and the app survives. A
Runnable handed to `getBridge().execute(...)` (or a bare `new Thread`) runs later, on another
thread, long after that try/catch has returned. There is no catch above it, so Android's default
uncaught-exception handler ends the PROCESS.

`nativeReport` was the one such task in FolderSyncPlugin with no guard of its own, and it is the one
the Folder Sync screen calls as it paints (`_readNativeLast` in sync.js). The screen asked what the
last sweep did, the answer threw, and the app was gone before it could draw.

The catch must also ANSWER the call. A background task that swallows and returns leaves the JS
promise pending for ever, which on this screen is a spinner that never resolves — the same report
with a different shape, and the harder one to tell from a hang.

This is a source audit rather than a device test because the failure is structural: the presence of
an unguarded body is the bug, and no amount of exercising finds the one input that throws.
"""
import re
import unittest
from pathlib import Path

SYNC = (Path(__file__).resolve().parent.parent / "mobile" / "android" / "app" / "src" / "main"
        / "java" / "place" / "poster" / "app" / "sync")


def _bodies(src: str, opener: str):
    """Every lambda/Runnable body started by `opener`, brace-matched."""
    out = []
    for m in re.finditer(re.escape(opener), src):
        i = src.index("{", m.end() - 1) if "{" in src[m.end() - 1:m.end() + 200] else -1
        # `execute(() -> oneCall(x));` has no brace — take to the end of the statement.
        semi = src.find(";", m.end())
        brace = src.find("{", m.end())
        if brace < 0 or (0 <= semi < brace):
            out.append((m.start(), src[m.end():semi + 1]))
            continue
        depth, k = 0, brace
        while k < len(src):
            if src[k] == "{":
                depth += 1
            elif src[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        out.append((m.start(), src[brace:k + 1]))
    return out


def _line(src: str, pos: int) -> int:
    return src.count("\n", 0, pos) + 1


class NoBackgroundTaskMayEndTheProcess(unittest.TestCase):
    FILES = sorted(SYNC.glob("*.java"))

    def test_the_package_has_not_moved(self):
        self.assertTrue(self.FILES, "no sync sources found — this audit would pass vacuously")

    def test_every_bridge_task_and_thread_body_is_wrapped(self):
        bad = []
        for path in self.FILES:
            src = path.read_text(encoding="utf-8")
            for opener in ("getBridge().execute(", "new Thread("):
                for pos, body in _bodies(src, opener):
                    if "try" not in body:
                        bad.append("%s:%d  %s" % (path.name, _line(src, pos),
                                                  body.strip().split("\n")[0][:70]))
        self.assertEqual(bad, [], "a throw in these ends the PROCESS, not the task:\n" + "\n".join(bad))

    def test_a_guarded_bridge_task_catches_throwable_not_only_exception(self):
        """`catch (Exception)` does not catch an Error — a missing class, a Keystore provider
        failure — and those are exactly the device-only faults there is no way to try here."""
        weak = []
        for path in self.FILES:
            src = path.read_text(encoding="utf-8")
            for pos, body in _bodies(src, "getBridge().execute("):
                if "try" not in body:
                    continue
                if "catch (Throwable" not in body and "catch (Exception" in body:
                    weak.append("%s:%d" % (path.name, _line(src, pos)))
        self.assertEqual(weak, [], "these catch Exception only:\n" + "\n".join(weak))

    def test_the_guard_is_the_OUTERMOST_catch_and_not_one_nested_inside_a_loop(self):
        """A `catch (Throwable)` around one statement deep inside the task satisfies a naive search
        while the task as a whole is still unguarded — a test that passes against broken code, which
        is the failure mode this file exists to avoid. The guard must sit at the task body's own
        level."""
        bad = []
        for path in self.FILES:
            src = path.read_text(encoding="utf-8")
            for pos, body in _bodies(src, "getBridge().execute("):
                if not body.startswith("{"):
                    continue
                m = re.search(r"catch \(Throwable", body)
                if not m:
                    bad.append("%s:%d  no Throwable guard at all" % (path.name, _line(src, pos)))
                    continue
                depth = body[:m.start()].count("{") - body[:m.start()].count("}")
                if depth != 1:
                    bad.append("%s:%d  guard is nested %d deep, so the task is still unguarded"
                               % (path.name, _line(src, pos), depth))
        self.assertEqual(bad, [], "\n".join(bad))

    def test_the_task_the_folder_sync_screen_calls_answers_even_when_it_fails(self):
        """`_readNativeLast` in sync.js calls this as the screen paints. Swallowing without an
        answer turns a crash into a spinner that never resolves."""
        src = (SYNC / "FolderSyncPlugin.java").read_text(encoding="utf-8")
        bodies = [b for _, b in _bodies(src, "getBridge().execute(")
                  if "nativeReportNow" in b]
        self.assertEqual(len(bodies), 1, "nativeReport's task moved or was duplicated")
        body = bodies[0]
        self.assertIn("catch (Throwable", body)
        self.assertIn("call.reject", body, "a failure must reach the page, not vanish")

    def test_the_screen_really_does_call_it_while_painting(self):
        """If this stops being true the test above is guarding the wrong method, so it is asserted
        rather than assumed."""
        js = (Path(__file__).resolve().parent.parent / "static" / "js" / "client"
              / "sync.js").read_text(encoding="utf-8")
        self.assertIn("fs.nativeReport()", js)
        self.assertIn("_readNativeLast", js)


if __name__ == "__main__":
    unittest.main()
