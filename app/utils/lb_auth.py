"""Trust for node-to-node ("load-balanced") calls — ONE place, so it can't drift per router.

A node forwards work to its peers (image/music/video/voice generation, torrents, the storage-server
role) as a plain HTTP call carrying no user session, because there is no user on the other side —
just another node doing the work. That was expressed as a bare `X-Posterchanai-Load-Balanced: true`
header, which is a header ANY caller can set: it was an authentication bypass on every endpoint that
honoured it, reachable by curl from the internet.

Two things this fixes, and they are separable:

1. A REQUEST WITH NO CREDENTIALS AT ALL IS NOT A PEER. Fifteen storage endpoints computed
   `is_server_request = current_user is None or header == "true"`, so an anonymous request — no
   cookie, no key, and not even the header — was granted the server's own trust and could name any
   `username` it liked (`GET /api/storage/view-file?username=victim&file_path=...`). Peer calls
   always send the header, so requiring it cannot break them. `save-image` already had this shape
   right; everything else now matches it.

2. THE HEADER IS PROOF OF NOTHING UNTIL IT CARRIES A SECRET. Set `lb_shared_secret` to the same
   value on every node and peers must prove it (`X-PosterChanAI-LB-Auth`, constant-time compare).
   Until it is set the header alone is still accepted, so an existing multi-node deployment keeps
   working after a deploy and turns the check on when the operator sets the value — a fix that
   silently broke every peer call the moment it shipped would just be reverted.
"""
import hmac

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
        return (settings_store.get("lb_shared_secret", "") or "").strip()
    except Exception:
        # A settings read must never turn the guard into a hard failure; "" only means we fall back
        # to the legacy header-only trust, never that a request is granted trust it didn't have.
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
        return True                    # not configured yet — legacy behaviour, see the module docstring
    try:
        got = (request.headers.get(_AUTH_HEADER, "") or "").strip()
    except Exception:
        return False
    return bool(got) and hmac.compare_digest(got, secret)
