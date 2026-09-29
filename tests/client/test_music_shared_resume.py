"""A car's play button resumes the SHARED playlist after Android reloaded the page, never your own
library in its place. Runs the shipped musicplayer.js — see music_shared_resume_runtime.cjs."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_a_cold_resume_keeps_the_shared_playlist():
    r = subprocess.run(["node", "tests/client/music_shared_resume_runtime.cjs"], cwd=ROOT,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[-2000:]
    out = json.loads(r.stdout)
    bad = {k: v["detail"] for k, v in out.items() if not v["ok"]}
    assert not bad, bad
    assert len(out) == 5


def test_the_share_module_exposes_what_the_player_uses():
    src = (ROOT / "static/js/client/musicshare.js").read_text()
    assert "register, meta, plain, shareOf, restore," in src
