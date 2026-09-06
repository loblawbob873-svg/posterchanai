"""Authenticated node-to-node calls. Missing or unreadable credentials fail closed.

Set lb_shared_secret to the same random value on participating nodes, or supply
POSTERCHANAI_LB_SHARED_SECRET through each node's protected environment file.
The routing flag alone never grants authorization.
"""
import hmac
import os

_FLAG_HEADER = "x-posterchanai-load-balanced"
_AUTH_HEADER = "x-posterchanai-lb-auth"

# Senders use these names (HTTP headers are case-insensitive; kept in the historical spelling so a
# node running older code still recognises the flag).
FLAG_HEADER_NAME = "X-Posterchanai-Load-Balanced"
AUTH_HEADER_NAME = "X-PosterChanAI-LB-Auth"


def shared_secret() -> str:
    """The configured peer secret, or "" when the operator hasn't set one."""
    try:
        from app.services import settings_store
        return (os.environ.get("POSTERCHANAI_LB_SHARED_SECRET") or
                settings_store.get("lb_shared_secret", "") or "").strip()
    except Exception:
        # A settings failure must not turn a public caller into a trusted peer.
        return ""


def headers(extra: dict | None = None) -> dict:
    """Headers for an outgoing peer call. Always the flag; the secret too once configured."""
    out = {FLAG_HEADER_NAME: "true"}
    secret = shared_secret()
    if secret:
        out[AUTH_HEADER_NAME] = secret
    if extra:
        out.update(extra)
    return out


def is_internal(request) -> bool:
    """True when this request is a peer node's call, and may therefore skip per-user authorization.

    Fail-closed on anything unexpected: no request object, no flag header, or a configured secret
    that doesn't match all return False, which drops the caller back to ordinary user auth.
    """
    if request is None:
        return False
    try:
        flag = (request.headers.get(_FLAG_HEADER, "") or "").strip().lower()
    except Exception:
        return False
    if flag != "true":
        return False
    secret = shared_secret()
    if not secret:
        return False
    try:
        got = (request.headers.get(_AUTH_HEADER, "") or "").strip()
    except Exception:
        return False
    return bool(got) and hmac.compare_digest(got, secret)
