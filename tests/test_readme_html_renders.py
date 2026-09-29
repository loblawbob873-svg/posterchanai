"""A repo README's HTML renders (links, images, relative images), and nothing unsafe gets through.

Reported: "on the ngit posterchan repo, it's showing HTML instead of rendering it" — the README's
<a href><img></a> rows came out as literal tags and its relative images were dropped.
tests/client/readme_html_runtime.cjs runs the shipped mdToHtml against the real README.md."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_readme_html_renders_safely():
    r = subprocess.run(["node", "tests/client/readme_html_runtime.cjs"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "OK readme html" in r.stdout, (r.stdout[-3000:], r.stderr[-3000:])
