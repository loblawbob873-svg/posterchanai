"""An app session with no server says so; a login answered by something other than the app server names it.

2026-10-14: a member's Monero wallet on the APK showed "Could not establish app session: login response did
not contain a credential" while every nostr-login in the server log was a complete 200. With "Use no server"
(relays only) the POST goes to the bundle itself, which answers 200 with its own page. Runs the SHIPPED
ensureAiSession: tests/client/ai_session_no_server_runtime.mjs.
"""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_no_server_is_said_and_a_foreign_answer_is_named():
    r = subprocess.run(["node", str(Path(__file__).with_name("ai_session_no_server_runtime.mjs"))],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stdout + r.stderr
