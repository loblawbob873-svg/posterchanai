"""`/r/<owner>/<repo>` — a repo's public page, served to anyone with no account.

The route's whole job is to be reachable and to fail SOFT: a repo with no announcement, an
unreachable relay, or an owner segment that resolves to nobody must still serve the app, which
routes client-side and can say what went wrong with the relay data it has. A 500 here is a broken
link for a human, not just a missing preview for a crawler.
"""

from fastapi.testclient import TestClient

import app.main as main

client = TestClient(main.app)

_NPUB = "npub1fdtthaqujtjcd6yfy7kt0zpkadyl9vvypq00s5nztnmche74d0tqv6uwwr"


def test_the_public_repo_page_serves_the_client():
    r = client.get("/r/%s/posterchanai" % _NPUB)
    assert r.status_code == 200
    assert "static/js/client/git.js" in r.text


def test_an_unreachable_relay_still_serves_the_page(monkeypatch):
    """The preview is best-effort. This is the case that decides whether a bad relay day is a
    missing picture or a dead link."""
    from app.services import git_share

    async def _boom(*a, **kw):
        raise RuntimeError("relay is down")

    monkeypatch.setattr(git_share, "repo_card", _boom)
    r = client.get("/r/%s/posterchanai" % _NPUB)
    assert r.status_code == 200
    assert "<title>PosterChan · Nostr</title>" in r.text   # no card, but a working page


def test_an_unresolvable_owner_still_serves_the_page():
    """Resolution happens in the CLIENT too, and it has relay hints this process may not. Refusing
    here would turn "I could not look it up" into "this repo does not exist"."""
    assert client.get("/r/definitely-not-a-key/whatever").status_code == 200


def test_a_repo_with_an_announcement_gets_a_real_card(monkeypatch):
    from app.services import git_share

    async def _card(port, owner_hex, repo_id):
        assert repo_id == "posterchanai"
        return {"name": "PosterChanAI", "author": "alice", "repo": repo_id,
                "description": "self-hosted everything", "image": "https://x/pic.png"}

    monkeypatch.setattr(git_share, "repo_card", _card)
    r = client.get("/r/%s/posterchanai" % _NPUB)
    assert r.status_code == 200
    assert 'property="og:title" content="PosterChanAI · alice"' in r.text
    assert 'property="og:description" content="self-hosted everything"' in r.text
    # og:url must be the URL that was actually shared, or a crawler canonicalises the preview onto
    # something else and the card links away from the repo.
    assert "/r/%s/posterchanai" % _NPUB in r.text


def test_the_identifier_reaches_the_lookup_EXACTLY_as_it_was_written(monkeypatch):
    """NOT lowercased. A repo id minted by this host is lowercase, but the page serves repos
    announced from anywhere and NIP-34 does not say a `d` tag is lowercase. Folding here would make
    `/r/<npub>/MyApp` look for an announcement tagged `myapp` and find nothing — and since the client
    now builds this URL from the `d` tag verbatim, that is a link the app hands out and then cannot
    open. (repo_card asks the relay for BOTH spellings; that is its job, not the route's.)"""
    from app.services import git_share
    seen = {}

    async def _card(port, owner_hex, repo_id):
        seen["id"] = repo_id
        return None

    monkeypatch.setattr(git_share, "repo_card", _card)
    assert client.get("/r/%s/MyApp" % _NPUB).status_code == 200
    assert seen["id"] == "MyApp"


def test_the_route_does_not_shadow_the_nostr_entity_catch_all():
    """`/r/…` is two segments and registered first; a bare entity URL must still open."""
    r = client.get("/" + _NPUB)
    assert r.status_code == 200
    assert client.get("/r").status_code == 404
