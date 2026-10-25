"""The "Restarts & memory" summary (settings.js _healthLine) says what ended the app, run under node on the shipped
function. The report shape is AppHealthPlugin.report()'s."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _line(report):
    src = (ROOT / "static/js/client/settings.js").read_text()
    m = re.search(r"\n  function _healthLine\(r\)\{.*?\n  \}\n", src, re.S)
    assert m, "_healthLine moved"
    js = m.group(0) + "\nprocess.stdout.write(_healthLine(%s));" % json.dumps(report)
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


pytestmark = pytest.mark.skipif(not shutil.which("node"), reason="node required")


def test_a_memory_reclaim_while_in_use_is_said_first():
    out = _line({"exitsSupported": True, "memory": {"totalMb": 3800, "availMb": 410, "lowMemory": True, "appPssMb": 640},
                 "exits": [{"at": 1761400000000, "reason": "low-memory", "unexpected": True, "onScreen": True, "pssMb": 812},
                           {"at": 1761300000000, "reason": "user-requested", "unexpected": False}]})
    assert "low-memory while you were using it" in out and "812 MB" in out, out
    assert "410 / 3800 MB free (LOW)" in out and "app now 640 MB" in out, out


def test_a_swipe_or_an_update_is_not_reported_as_a_restart():
    out = _line({"exitsSupported": True, "memory": {}, "exits": [{"reason": "package-updated", "unexpected": False}]})
    assert "no unexpected restarts recorded" in out, out


def test_old_android_says_it_cannot_tell():
    assert "too old" in _line({"exitsSupported": False, "memory": {}, "exits": []})
