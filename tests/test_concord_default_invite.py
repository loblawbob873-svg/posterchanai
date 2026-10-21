"""The instance community's invite is handed to MEMBERS only, and only when it is an invite link.

It carries the community's # secret, so it must never be in the public client config; the route sits
behind the same member dependency as the tunnel ticket. A setting that is not an invite link (a typo,
a bare naddr, a pasted nsec — Admin -> Bots has seen that) is answered as "none" rather than handed out.
"""
import asyncio

from app.routers import concord_tunnel
from app.services import settings_store


def _ask(monkeypatch, value):
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: value if k == "concord_default_invite" else d)
    return asyncio.run(concord_tunnel.default_invite(user=object()))


def test_a_valid_invite_is_returned(monkeypatch):
    url = "https://poster.place/invite/naddr1qqhm#secret"
    assert _ask(monkeypatch, url) == {"invite": url}


def test_anything_else_is_none(monkeypatch):
    for bad in ("", None, "naddr1qqhm", "nsec1" + "q" * 58, "https://poster.place/invite/naddr1qqhm", "javascript:alert(1)/invite/#x"):
        assert _ask(monkeypatch, bad) == {"invite": ""}, bad


def test_the_route_is_members_only():
    route = next(r for r in concord_tunnel.router.routes if r.path.endswith("/default-invite"))
    deps = [d.call for d in route.dependant.dependencies]
    assert concord_tunnel._member in deps, "the invite (with its secret) must not be served to non-members"


def test_the_setting_is_declared_and_has_an_admin_field():
    from pathlib import Path
    from app.schemas import SettingsResponse
    assert "concord_default_invite" in SettingsResponse.model_fields
    html = (Path(__file__).resolve().parents[1] / "templates/admin/tabs/nostr_relay.html").read_text()
    assert 'id="concord_default_invite" name="concord_default_invite"' in html
