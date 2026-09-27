"""A mention of an account with no known profile shows a short npub, never "@profile".
Run: venv-unified/bin/python -m pytest tests/client/test_mention_label.py"""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_an_unknown_mention_is_labelled_by_its_npub():
    r = subprocess.run(["node", os.path.join(HERE, "mention_label_runtime.mjs")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
