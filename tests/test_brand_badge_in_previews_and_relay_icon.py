"""Link previews and the relay's icon show the round PosterChan badge ("switch to the new badge", 2026-10-29).

When a repo or profile link has no picture of its own, its OpenGraph card falls back to the app's mark; a Nostr client
showing this relay (NIP-11 `icon`, e.g. Yakihonne) does the same. Both were the old hooded portrait
(posterchan-relay.png); they are the app icon now -- static/icon-512.png, 512px, large enough for a link card. Driven
through the real routes and the real NIP-11 document, with only the relay reads stubbed.
"""
import asyncio
import json
from pathlib import Path

from app.services.nostr_relay.server import RelayServer

ROOT = Path(__file__).resolve().parents[1]
ICON = "/static/icon-512.png"


class _Req:
    headers = {"host": "example.test", "x-forwarded-proto": "https"}
    url = type("u", (), {"scheme": "https"})()
    base_url = "https://example.test/"


def test_the_relay_icon_is_the_badge():
    s = object.__new__(RelayServer)
    s.cfg = {"wot_enabled": False}
    assert json.loads(s.nip11_doc("relay.example"))["icon"] == "https://relay.example" + ICON
    assert (ROOT / "static/icon-512.png").is_file()


def test_link_previews_without_a_picture_fall_back_to_the_badge(monkeypatch):
    import app.main as main
    from app.services import git_share, profile_share
    import app.routers.client as client
    seen = []

    async def shell(request, user, meta=None):
        seen.append(meta)
        return "ok"
    monkeypatch.setattr(client, "render_client_shell", shell)
    monkeypatch.setattr(git_share, "resolve_owner", lambda o: "a" * 64)
    monkeypatch.setattr(git_share, "valid_url_repo_id", lambda r: True)

    async def repo_card(port, owner, repo):
        return {"repo": repo, "name": "My App", "description": "does things"}
    monkeypatch.setattr(git_share, "repo_card", repo_card)

    async def profile_card(port, who):
        return {"name": "Alice", "about": "hi"}
    monkeypatch.setattr(profile_share, "profile_card", profile_card)

    asyncio.run(main.git_repo_page("npub1alice", "my-app", _Req()))
    asyncio.run(main.profile_at_page("alice", _Req()))
    assert len(seen) == 2 and all(seen), seen
    for meta in seen:
        assert meta.get("image", "").endswith(ICON), ("a link preview without a picture is not the badge", meta)
