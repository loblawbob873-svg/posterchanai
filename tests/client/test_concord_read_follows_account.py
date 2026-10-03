"""Concord mentions read on one device are read on all of them (see the .mjs for the report)."""
import subprocess
from pathlib import Path


def test_concord_read_follows_the_account():
    script = Path(__file__).with_name("concord_read_follows_account_runtime.mjs")
    r = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_read_doc_is_pinned_and_carried():
    """Every private doc here has missed one of these at least once; the symptom is the default."""
    root = Path(__file__).resolve().parents[2]
    assert "t[1] === 'pcai:concord-read'" in (root / "static/js/client/store.js").read_text()
    assert "/^pcai:concord-read$/" in (root / "static/js/client/app.js").read_text()
