"""No client module starts a long timer just by being loaded -- unless that decision is written down here.

2026-10-03: os.js armed a 15-second Startup Apps fallback at LOAD, everywhere. Nothing broke in a
browser, but every node test that loads os.js (the widget tests, the desktop layout tests) then waited
15 seconds to exit: test_desktop_widgets went from 5 seconds to more than 300, a targeted run looked
hung, and every deploy gate got slower by minutes. Nothing failed -- the cost was only time, which is
the kind of regression no assertion catches.

So, like the private-documents rule: every module that arms an interval, or a timeout of 3s or more,
during load is listed below with the reason. A new one fails this test until somebody decides. The
probe runs each module in a vm against a permissive fake page (every feature check answers "present"),
so a timer guarded by a feature check -- os.js's, which only arms inside the PosterChanOS shell --
still shows here; the reason says so.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
CLIENT = HERE.parents[1] / "static" / "js" / "client"
LONG_MS = 3000

ALLOWED = {
    ("app.js", "interval", 4000): "healNav: the nav repair sweep the client has always run",
    ("concord.js", "interval", 20000): "SWEEP_MS: the background sweep for rooms not on screen (mentions, unread)",
    ("instance-welcome.js", "interval", 10000): "re-checks a pending NIP-05 application while the welcome screen waits",
    ("news.js", "timeout", 4000): "first background news tick, a few seconds after boot",
    ("news.js", "interval", 600000): "background news refresh every 10 minutes",
    ("notes.js", "interval", 300000): "flushes notes typed offline, every 5 minutes",
    ("os.js", "timeout", 15000): "Startup Apps offline fallback -- armed only inside the PosterChanOS shell (window.pcShell); "
                                 "the probe's fake page answers yes to that check",
    ("sms.js", "interval", 3000): "Texts: polls the native SMS bridge while the app is open",
    ("sync.js", "interval", 5000): "Folder Sync's scheduler tick",
    ("telegram.js", "timeout", 4000): "first background Telegram check, a few seconds after boot",
    ("vault.js", "interval", 300000): "password vault auto-lock check",
}


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_every_timer_started_at_load_is_a_recorded_decision():
    r = subprocess.run(["node", str(HERE / "client_timers_at_load_probe.js"), str(CLIENT)],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    seen = json.loads(r.stdout)
    found = {(mod, kind, ms) for mod, v in seen.items() for kind, ms in v["timers"]
             if kind == "interval" or ms >= LONG_MS}
    new = sorted(found - set(ALLOWED))
    assert not new, ("a module now starts a long timer just by being loaded -- every node test that loads it "
                     "waits it out. Arm it on first use, or add it to ALLOWED with the reason: %r" % new)
    gone = sorted(set(ALLOWED) - found)
    assert not gone, "these load-time timers are gone -- take them off the list so it stays true: %r" % gone
