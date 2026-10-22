"""A refused sign-in is remembered, not re-asked every minute (see the .mjs for the report)."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_a_refused_sign_in_is_remembered_and_explained():
    r = subprocess.run(["node", str(Path(__file__).with_name("ai_session_refusal_runtime.mjs"))],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr


def test_telegram_shows_the_refusal_instead_of_not_authenticated():
    tg = (ROOT / "static/js/client/telegram.js").read_text()
    assert "catch(e){ if(e && (e.offline || e.refused)) throw e; }" in tg
