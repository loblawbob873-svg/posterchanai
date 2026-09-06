"""Two authorization holes that both let one user read another user's files, and the guards for them.

1. NODE-TO-NODE TRUST (app/utils/lb_auth.py). A node forwards work to its peers with no user session,
   and proved it was a node by setting `X-Posterchanai-Load-Balanced: true` — a header any caller can
   set with curl. Worse, fifteen storage endpoints wrote `is_server_request = current_user is None or
   header == "true"`, so a request with NO credentials at all was granted the server's own trust and
   could name any `username` it liked. `GET /api/storage/view-file?username=victim&file_path=...` was
   an unauthenticated read of anybody's files.

2. THUMBNAIL PATH CONTAINMENT (app/routers/files.py). Containment was `str(full).startswith(str(base))`
   with no separator, so `<base>/dave/../dave.smith/x` passes the check for base `<base>/dave`. Any
   username that is a prefix of another was a read of that user's files.

These assert the source-level shape rather than driving the app, because both bugs were single
expressions that read as correct — and both would come back the same way.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STORAGE = ROOT / "app" / "routers" / "storage.py"
FILES = ROOT / "app" / "routers" / "files.py"


# --- 1. node-to-node trust -----------------------------------------------------------------------

def test_no_endpoint_treats_an_anonymous_request_as_a_peer_node():
    """`current_user is None` must never contribute to server trust: no credentials is not a peer."""
    offenders = []
    for path in sorted((ROOT / "app" / "routers").glob("*.py")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"is_server_request\s*=.*current_user is None", line):
                offenders.append(f"{path.name}:{i}")
    assert not offenders, (
        "these grant server-to-server trust to a request that presented NO credentials, so any "
        "anonymous caller may name another user's `username`: " + ", ".join(offenders))


def test_routers_gate_peer_trust_through_lb_auth_not_a_raw_header_read():
    """A raw `== "true"` compare is the bypass; the shared-secret check lives in lb_auth.is_internal.

    Two files are exempt and only these two: openai_api and image_api read the header to decide
    whether to RE-BALANCE onward (loop prevention — a peer's request must be served locally, not
    forwarded again). That is behaviour, not authorization, and no access follows from it.
    """
    allowed_behaviour_only = {"openai_api.py", "image_api.py"}
    offenders = []
    for path in sorted((ROOT / "app" / "routers").rglob("*.py")):
        if path.name in allowed_behaviour_only:
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if 'x-posterchanai-load-balanced"' in line.lower() and "headers.get" in line:
                offenders.append(f"{path.name}:{i}")
    assert not offenders, (
        "these authorize on a header any caller can set instead of lb_auth.is_internal(request): "
        + ", ".join(offenders))


def test_is_internal_requires_the_flag_header():
    from app.utils import lb_auth
    assert lb_auth.is_internal(_Req({})) is False
    assert lb_auth.is_internal(None) is False


def test_is_internal_rejects_the_bare_flag_when_no_secret_is_configured():
    """Unconfigured peer trust fails closed; configure all nodes before rollout."""
    from app.utils import lb_auth
    with _secret(""):
        assert lb_auth.is_internal(_Req({"x-posterchanai-load-balanced": "true"})) is False


def test_a_configured_secret_makes_the_bare_flag_worthless():
    from app.utils import lb_auth
    with _secret("s3cret-value"):
        assert lb_auth.is_internal(_Req({"x-posterchanai-load-balanced": "true"})) is False
        assert lb_auth.is_internal(_Req({
            "x-posterchanai-load-balanced": "true",
            "x-posterchanai-lb-auth": "wrong"})) is False
        assert lb_auth.is_internal(_Req({
            "x-posterchanai-load-balanced": "true",
            "x-posterchanai-lb-auth": "s3cret-value"})) is True


def test_senders_carry_the_secret_so_peer_calls_keep_working_once_it_is_set():
    from app.utils import lb_auth
    with _secret(""):
        assert lb_auth.headers() == {"X-Posterchanai-Load-Balanced": "true"}
    with _secret("s3cret-value"):
        h = lb_auth.headers()
        assert h["X-Posterchanai-Load-Balanced"] == "true"
        assert h["X-PosterChanAI-LB-Auth"] == "s3cret-value"
        # A sender that builds its own dict must not lose the auth header.
        assert lb_auth.headers({"X-API-Key": "k"})["X-PosterChanAI-LB-Auth"] == "s3cret-value"


def test_every_peer_call_site_uses_the_shared_header_builder():
    """A hand-written {"X-Posterchanai-Load-Balanced": "true"} would silently omit the secret and
    start failing the moment an operator configures one — the LB breaking with no error to explain it."""
    offenders = []
    for sub in ("services", "routers"):
        for path in sorted((ROOT / "app" / sub).rglob("*.py")):
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if '"X-Posterchanai-Load-Balanced"' in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{i}")
    assert not offenders, ("these build the peer header by hand instead of lb_auth.headers(), so "
                          "they will not carry the secret: " + ", ".join(offenders))


# --- 2. thumbnail path containment ---------------------------------------------------------------

def test_thumbnail_containment_uses_the_validator_not_a_prefix_compare():
    src = FILES.read_text(encoding="utf-8")
    assert "startswith(str(user_path.resolve()))" not in src, (
        "a prefix compare with no separator lets `<base>/dave/../dave.smith` pass containment for "
        "base `<base>/dave` — use _validate_path_within_base, as view_file does")
    assert "_validate_path_within_base(full_path, user_path)" in src


def test_the_validator_actually_rejects_a_prefix_sibling(tmp_path):
    """The property the endpoint depends on — asserted against the real helper, not its name."""
    from app.services.storage_service import _validate_path_within_base
    base = tmp_path / "dave"
    sibling = tmp_path / "dave.smith"
    base.mkdir()
    sibling.mkdir()
    victim = sibling / "passport.jpg"
    victim.write_bytes(b"x")

    escaped = (base / ".." / "dave.smith" / "passport.jpg").resolve()
    assert escaped == victim.resolve()                       # the traversal really does land there
    assert _validate_path_within_base(escaped, base) is False
    assert _validate_path_within_base(base / "own.jpg", base) is True


# --- helpers -------------------------------------------------------------------------------------

class _Req:
    """Minimal stand-in for a Starlette request: case-insensitive headers are all is_internal reads."""
    def __init__(self, headers):
        self.headers = {k.lower(): v for k, v in headers.items()}


class _secret:
    """Pin what lb_auth.shared_secret() returns, without touching the real settings cache."""
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        from app.services import settings_store
        self._orig = settings_store.get
        settings_store.get = lambda key, default=None: (
            self.value if key == "lb_shared_secret" else self._orig(key, default))
        return self

    def __exit__(self, *exc):
        from app.services import settings_store
        settings_store.get = self._orig
        return False
