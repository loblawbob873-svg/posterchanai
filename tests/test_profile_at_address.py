"""`poster.place/@<name>` -- a person's readable address, with a real link preview.

"any way for local users to resolve their profile page like https://poster.place/verita84 like the
fediverse does it?" A bare /<name> would collide with the app's own pages; /@name is the fediverse's own
form and nothing else starts with @. Runs the shipped route function, the shipped client path decoder
and the shipped actor builder.
"""
import asyncio
import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HEX = "4b56bbf41c92e586e88927acb78836eb49f2b184081ef852625cf78be7d56bd6"


class _Req:
    def __init__(self, accept="text/html"):
        self.headers = {"accept": accept, "host": "poster.place", "x-forwarded-proto": "https"}
        self.url = type("u", (), {"scheme": "http"})()
        self.base_url = "http://poster.place/"


@pytest.fixture
def page(monkeypatch):
    import app.main as m
    from app.routers import client
    from app.services import git_share, profile_share
    seen = {}

    async def shell(request, db, meta=None):
        seen["meta"] = meta
        return "SHELL"
    monkeypatch.setattr(client, "render_client_shell", shell)
    monkeypatch.setattr(git_share, "resolve_owner", lambda n: HEX if n in ("verita84", "verita84@poster.place") else None)

    async def card(port, hx):
        assert hx == HEX
        return {"name": 'Verita "the" 84', "about": "Nostr's #1 <b>Bad Actor</b>",
                "picture": "https://media.poster.place/a.png"}
    monkeypatch.setattr(profile_share, "profile_card", card)
    return m, seen


def test_a_granted_name_gets_a_real_card(page):
    m, seen = page
    assert asyncio.run(m._profile_page("verita84", _Req(), "/@")) == "SHELL"
    meta = seen["meta"]
    assert meta["title"] == 'Verita "the" 84 (@verita84)'
    assert meta["url"] == "https://poster.place/@verita84", "og:url must be the address the reader is on"
    assert meta["image"] == "https://media.poster.place/a.png" and meta["type"] == "profile"
    assert "Bad Actor" in meta["description"]


def test_an_unknown_name_still_serves_the_app(page):
    m, seen = page
    assert asyncio.run(m._profile_page("nobody", _Req(), "/@")) == "SHELL"
    assert seen["meta"] is None


def test_the_fediverse_is_sent_to_the_actor(page):
    m, _ = page
    r = asyncio.run(m._profile_page("verita84", _Req("application/activity+json"), "/@"))
    assert r.status_code == 301 and r.headers["location"] == "/ap/users/verita84"


def test_the_route_sits_before_the_catch_all_that_would_404_it():
    import app.main as m
    paths = [getattr(r, "path", "") for r in m.app.routes]
    assert "/@{name}" in paths, "no /@<name> route"
    assert paths.index("/@{name}") < paths.index("/{entity}"), "/{entity} would answer /@name with a 404"


def test_the_card_text_is_escaped_into_head():
    """Anyone writes their own kind 0: a quote in a display name must not close the attribute."""
    from tests.test_git_archive_and_share import _render_shell
    from app.services import profile_share
    meta = profile_share.og_meta({"name": 'Eve" onload="x', "about": "<script>1</script>", "picture": ""},
                                 "eve", "https://poster.place/@eve")
    html = _render_shell(meta)
    assert 'onload="x' not in html and "<script>1</script>" not in html


def test_a_picture_that_is_not_https_never_reaches_head():
    from app.services import profile_share

    async def q(port, f, timeout=8.0):
        return True, {"content": json.dumps({"name": "x", "picture": "javascript:alert(1)"})}
    import app.services.fedi_bridge_identity as fbi
    orig = fbi.query_one
    fbi.query_one = q
    try:
        card = asyncio.run(profile_share.profile_card(3052, HEX))
    finally:
        fbi.query_one = orig
    assert card["picture"] == ""


def test_the_client_opens_a_profile_for_an_at_address():
    src = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
    i = src.index("function _entityFromPath(){")
    depth, j = 0, src.index("{", i)
    for k in range(j, len(src)):
        depth += src[k] == "{"
        depth -= src[k] == "}"
        if depth == 0:
            body = src[i:k + 1]
            break
    js = ("const location={pathname:''};function _entityFromQuery(){return null}\n" + body +
          "\nconst out={};for(const p of ['/@verita84','/@verita84@poster.place','/client/@bob','/@a/b','/users/x','/admin'])"
          "{location.pathname=p;out[p]=_entityFromPath();}process.stdout.write(JSON.stringify(out));")
    out = json.loads(subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout)
    assert out["/@verita84"] == {"kind": "user", "q": "verita84"}
    assert out["/@verita84@poster.place"] == {"kind": "user", "q": "verita84@poster.place"}
    assert out["/client/@bob"] == {"kind": "user", "q": "bob"}
    assert out["/@a/b"] is None and out["/admin"] is None


def test_a_fediverse_view_profile_lands_on_the_readable_address():
    from app.services.activitypub import convert
    doc = convert.person(base="https://poster.place", name="verita84", profile={"name": "v"}, public_key_pem="k")
    assert doc["url"] == "https://poster.place/@verita84"
