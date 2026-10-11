"""The account's read markers (`pcai:seen`, static/js/client/seen.js) -- see seen_sync_runtime.mjs.

Read on one device is read on all of them: max-merged, never written from a read that did not answer,
per account. The full-app half is test_read_state_follows_the_account_full_app.py."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_seen_engine_runtime():
    r = subprocess.run(["node", str(Path(__file__).with_name("seen_sync_runtime.mjs"))],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_seen_doc_is_pinned_and_carried():
    """Every private doc here has missed one of these at least once; the symptom is the default --
    here, every notification and DM counted as new again."""
    assert "t[1] === 'pcai:seen'" in (ROOT / "static/js/client/store.js").read_text()
    assert "/^pcai:seen$/" in (ROOT / "static/js/client/app.js").read_text()


def test_seen_js_ships_before_app_js_and_is_precached():
    html = (ROOT / "templates/client.html").read_text()
    assert html.index("/static/js/client/seen.js") < html.index("/static/js/client/app.js")
    assert "'/static/js/client/seen.js'" in (ROOT / "static/js/client/sw.js").read_text()
    # the signer may sign it: kind 30078 is on the allowlist the local signer checks
    assert re.search(r"\b30078\b", (ROOT / "static/js/client/signer.js").read_text())
