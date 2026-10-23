"""The desktop build's "Audit bundled Concord surface" step agrees with the page it audits.

2026-10-23: cord-reader.js became an on-demand module (no <script> tag; -1.1 MB per cold load), but the audit still
ran `grep -q 'static/js/client/cord-reader.js' www/index.html` -- every desktop build failed on it (6e9f80176) while
every test in the repo passed. www/index.html is built from templates/client.html, so each script the audit requires
in (or forbids from) the page must be in (or absent from) client.html's tags.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WF = (ROOT / ".github/workflows/desktop.yml").read_text()
PAGE = (ROOT / "templates/client.html").read_text()
TAGS = set(re.findall(r'<(?:script|link)[^>]*(?:src|href)="/?(static/[^"?]+)', PAGE))


def test_every_page_tag_the_audit_requires_is_in_the_page():
    required = re.findall(r"^\s*grep -q '(static/[^']+)' www/index\.html", WF, re.M)
    assert required, "the audit no longer checks the page's tags -- this test reads nothing"
    missing = [r for r in required if r not in TAGS]
    assert not missing, ("the desktop audit requires page tags client.html does not have", missing)


def test_every_page_tag_the_audit_forbids_is_absent():
    forbidden = re.findall(r"^\s*! grep -q '(static/[^']+)' www/index\.html", WF, re.M)
    present = [f for f in forbidden if f in TAGS]
    assert not present, ("the desktop audit forbids page tags client.html has", present)
