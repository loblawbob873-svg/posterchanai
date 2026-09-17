"""Every screen the desktop can open has a heading a person can read.

The heading map fell back to the raw view id, so every System Settings window was headed
"__ossettings" and every Terminal window "terminal" (measured on the test desktop). The map is
evaluated under node and checked against the sidebar's own views and oswin.js's extra screens,
never a copied list.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
HTML = (ROOT / "templates/client.html").read_text(encoding="utf-8")
OSWIN = (ROOT / "static/js/client/oswin.js").read_text(encoding="utf-8")


def _heading_map():
    start = APP.index("$('#view-title').textContent = {")
    brace = APP.index("{", start + len("$('#view-title').textContent = "))
    end = APP.index("}[v]||v;", brace)
    return APP[brace:end + 1]


@pytest.mark.skipif(not shutil.which("node"), reason="node is required")
def test_no_view_is_headed_by_its_internal_id():
    keys = json.loads(subprocess.check_output(
        ["node", "-e", "process.stdout.write(JSON.stringify(Object.keys(%s)))" % _heading_map()], text=True))
    sidebar = set(re.findall(r'data-view="([a-z_-]+)"', HTML))
    extras = set(json.loads(re.search(r"const EXTRA_VIEWS = (\[[^\]]*\]);", OSWIN).group(1).replace("'", '"')))
    assert sidebar and extras
    missing = sorted((sidebar | extras) - set(keys))
    assert not missing, "these screens would be headed by their raw id: %s" % missing
