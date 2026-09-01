"""Admin's registration switch is durable, live, and enforced on every public signup path."""
from pathlib import Path

from app.schemas import SettingsResponse
from app.services import registration_service, settings_store

ROOT = Path(__file__).resolve().parents[1]


def test_registration_setting_defaults_open_and_parses_admin_response():
    assert SettingsResponse().registration_enabled == "true"
    database = (ROOT / "app/database.py").read_text()
    assert '"registration_enabled": "true"' in database


def test_registration_decision_reads_live_hydrated_cache(monkeypatch):
    values = {"registration_enabled": "true"}
    monkeypatch.setattr(settings_store, "get", lambda key, default=None: values.get(key, default))
    assert registration_service.enabled()
    values["registration_enabled"] = "false"
    assert not registration_service.enabled(), "the Admin toggle should apply without a restart"


def test_admin_control_and_client_shell_are_wired():
    admin = (ROOT / "templates/admin/tabs/site_settings.html").read_text()
    shell = (ROOT / "templates/client.html").read_text()
    route = (ROOT / "app/routers/client.py").read_text()
    assert 'id="registration_enabled" name="registration_enabled"' in admin
    assert "registration_enabled|default(true)" in shell
    # The shell is TOLD the answer; how it is computed is registration_service's business. This
    # named the inline settings_store parse, which was a second copy of the service's own rule —
    # so the test passed precisely while the duplication it pinned was the problem.
    assert '"registration_enabled": registration_service.enabled()' in route
    assert "from app.services import registration_service" in route


def test_every_public_user_creation_path_checks_the_switch():
    auth = (ROOT / "app/routers/auth.py").read_text()
    social = (ROOT / "app/routers/social_login.py").read_text()
    client = (ROOT / "app/routers/client.py").read_text()
    assert auth.count("registration_service.enabled()") >= 1
    assert social.count("registration_service.enabled()") >= 2
    signup = client[client.index("async def signup_follow("):client.index("class ClaimAdmin", client.index("async def signup_follow("))]
    assert "registration_service.enabled()" in signup
    assert "status_code=403" in signup
    claim = client[client.index("async def claim_nip05("):client.index("@router.get(\"/admin-nip05\"")]
    assert claim.index("if existing:") < claim.index("registration_service.enabled()")
    assert "status_code=403" in claim


def test_a_blank_value_means_not_configured_not_closed():
    """THE TRAP THIS DEPLOYMENT HAS ALREADY PAID FOR ONCE.

    `settings_store.get(key, default)` returns the STORED value even when it is empty, so a row
    written blank — an admin save of an untouched field, a migration, a hand-edited settings
    document — is not the default. Parsed as a boolean, "" is false: registration would have closed
    for the whole node with nothing anywhere saying why.

    That is exactly how a blank `searxng_enabled` once turned web search off node-wide. Registration
    is OPEN by default; closing it is a decision somebody makes, never one a deployment falls into.
    """
    from app.services import registration_service, settings_store

    original = settings_store.get
    try:
        for blank in ("", "   ", None):
            settings_store.get = lambda k, d=None, _v=blank: _v
            assert registration_service.enabled() is True, f"a {blank!r} value closed registration"
        for closed in ("false", "0", "off", "no"):
            settings_store.get = lambda k, d=None, _v=closed: _v
            assert registration_service.enabled() is False, f"{closed!r} did not close registration"
        for opened in ("true", "1", "on", "yes", "TRUE", " on "):
            settings_store.get = lambda k, d=None, _v=opened: _v
            assert registration_service.enabled() is True, f"{opened!r} did not open registration"
    finally:
        settings_store.get = original


def test_admin_creation_paths_are_deliberately_not_gated():
    """Named so nobody "completes" this feature by adding the guard to them.

    An admin granting caps, stream access or AI access creates the account it grants to, and an
    operator adding a user is not registration. Those routes are `_verify_admin_auth`-gated; adding
    the signup guard would break admin account creation on a closed server.
    """
    route = (ROOT / "app/routers/client.py").read_text()
    # Anchored on the signature: a bare name also matches `stream_access_status`, a read-only
    # route that neither creates a user nor should be admin-gated.
    for fn in ("async def user_caps_set(data", "async def stream_access(data",
               "async def ai_access(data", "async def bridge_access_grant(data"):
        body = route[route.index(fn):]
        body = body[:body.index("\n@router.") if "\n@router." in body else len(body)]
        assert "_verify_admin_auth" in body, f"{fn} is no longer admin-gated"
        assert "registration_service" not in body, (
            f"{fn} now refuses when signup is closed — an admin adding a user is not registration")
