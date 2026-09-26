"""Admin → Bots: a picked avatar file becomes the bot's avatar URL (runs admin_bot_avatar_runtime.mjs).

Run: venv-unified/bin/python -m pytest tests/client/test_admin_bot_avatar.py
"""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_a_picked_avatar_is_uploaded_and_saved():
    r = subprocess.run(["node", os.path.join(HERE, "admin_bot_avatar_runtime.mjs")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
