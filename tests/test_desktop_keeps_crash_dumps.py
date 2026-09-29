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
