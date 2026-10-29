"""The root icon addresses answer with the shipped icon.

poster.place, 2026-10-29: ~500 404s in two hours for /favicon.ico, /favicon.png and /apple-touch-icon.png -- browsers,
link-preview fetchers and feed readers ask the root whatever the page declares. GET and HEAD, through the real app.
"""
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def test_every_root_icon_is_the_shipped_picture():
    from app.main import app, _ROOT_ICONS
    c = TestClient(app)
    for name, file in _ROOT_ICONS.items():
        r = c.get("/" + name)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png", (name, r.status_code)
        assert r.content == (ROOT / "static" / file).read_bytes(), name
        assert c.head("/" + name).status_code == 200, name
    assert {"favicon.ico", "favicon.png", "apple-touch-icon.png"} <= set(_ROOT_ICONS)
