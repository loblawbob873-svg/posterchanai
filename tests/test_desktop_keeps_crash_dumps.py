"""A renderer segfault (exit 139) must leave a minidump behind, locally. Measured 2026-09-28 on a
PosterChanOS desktop: one native crash took six windows down and left only "crashed 139" — nothing to
say where it happened, because the crash reporter was never started."""
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "desktop/main.js").read_text()


def test_the_crash_reporter_is_started_locally_before_ready():
    i = SRC.index("crashReporter.start(")
    call = SRC[i:SRC.index(")", i) + 1]
    assert "uploadToServer: false" in call, "crash dumps must never leave the machine"
    ready = min(x for x in (SRC.find("app.whenReady"), SRC.find("app.on('ready'")) if x > 0)
    assert i < ready, "crashReporter.start after ready does nothing for the first crash"


def test_a_handler_that_cannot_run_never_costs_the_desktop():
    """The 2026-09-29 ISO gate: the package installed chrome_crashpad_handler 0644, Chromium's spawn of it
    was FATAL, and the desktop never drew. The reporter starts only behind an exec check."""
    i = SRC.index("crashReporter.start(")
    guard = SRC.rfind("if (_crashHandlerRunnable())", 0, i)
    assert guard != -1 and i - guard < 200, "crashReporter.start is not behind the handler check"
    body = SRC[SRC.index("function _crashHandlerRunnable()"):]
    body = body[:body.index("\n}\n")]
    assert "X_OK" in body and "chrome_crashpad_handler" in body


def test_the_package_makes_the_handler_executable():
    from pathlib import Path as _P
    root = _P(__file__).resolve().parents[1] / "os/overlay/app-misc/posterchan-desktop"
    (ebuild,) = sorted(root.glob("posterchan-desktop-*.ebuild"))
    assert "fperms 0755 /opt/posterchan/chrome_crashpad_handler" in ebuild.read_text(), \
        "doins leaves the handler 0644 and the desktop dies at startup"
