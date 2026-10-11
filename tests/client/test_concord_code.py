"""Concord renders `inline code` and fenced ``` blocks (runs the shipped concord.js under node)."""
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))


def test_concord_renders_code():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    r = subprocess.run([node, os.path.join(HERE, "concord_code_runtime.mjs")], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
