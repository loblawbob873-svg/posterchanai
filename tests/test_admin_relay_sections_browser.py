"""Runs scripts/check_admin_relay_sections.py (the shipped admin page in a real Chrome at 390px and 1280px):
the Relay tab's sections, Limits/Database contents, the Database status panel, and a Save round trip of a moved
field. Exit 2 from the check means it could not run here (no Chrome) and is a skip, never a pass."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_relay_sections_in_a_real_browser(tmp_path):
    env = dict(os.environ, PC_CHECK_SHOTS=str(tmp_path / "shots"))
    r = subprocess.run([sys.executable, str(ROOT / "scripts/check_admin_relay_sections.py")], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=240)
    if r.returncode == 2:
        pytest.skip(r.stdout.strip() or r.stderr.strip()[-300:])
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]


def test_a_chrome_slow_to_open_its_port_is_waited_for_not_skipped(tmp_path):
    """CI run 38661902280 aborted the desktop build: with ~100 browser checks beside it Chrome took more than
    the 20 s this check allowed to open its debugging port, the check answered SKIP, and a skipped REQUIRED
    test is an abort. A Chrome that starts 25 s late must still be driven to a verdict."""
    import shutil
    real = next((shutil.which(x) for x in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable")
                 if shutil.which(x)), None)
    if not real:
        pytest.skip("no Chrome")
    slow = tmp_path / "bin"
    slow.mkdir()
    wrapper = slow / "chromium"
    wrapper.write_text(f"#!/bin/sh\nsleep 25\nexec {real} \"$@\"\n")
    wrapper.chmod(0o755)
    env = dict(os.environ, PATH=f"{slow}:{os.environ.get('PATH', '')}", PC_CHECK_SHOTS=str(tmp_path / "shots"))
    r = subprocess.run([sys.executable, str(ROOT / "scripts/check_admin_relay_sections.py")], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode != 2, ("a slow Chrome was reported as no Chrome at all", r.stdout[-500:])
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
