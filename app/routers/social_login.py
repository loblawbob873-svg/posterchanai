"""Sign in with a fediverse or Google account — the two SERVER-MINTED identity routes.

Everywhere else in this app the Nostr key is made in the browser and never leaves it (see the note at
the top of routers/client.py). These two routes are the deliberate exception, for people who want an
account without managing a key: the node mints the keypair, stores it on the User row like any other
app account, and hands it back on each sign-in. That is a custodial identity and the login screen says
so — the operator can read those keys, which is exactly why both providers are OFF by default and why
nothing here touches an identity the user brought themselves.

Shape of a login, both providers:

    /api/auth/<p>/start   → redirect to the provider
    /api/auth/<p>/callback → the provider redirects back; we resolve the external identity, find or
                             create the User, then redirect to /client?login=<code>
    POST /api/auth/handoff {code} → the nsec, ONCE

The secret goes over that last POST rather than in the redirect URL: a query string or fragment lands
in browser history, in the referrer of anything the page loads, and in every proxy log on the way.
The code is single-use, expires in two minutes, and is bound to nothing else — it is only useful to
the browser that was just redirected.

`link` is the other direction: a user who ALREADY has a key attaches Google to it, so they can sign in
with Google later. That uploads their secret key to this node, so it is opt-in, authenticated with
their own key (the same self-proof every other /client write uses), and refuses to overwrite anything.
"""

import html
import logging
import secrets
import time
import urllib.parse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/auth", tags=["social-login"])

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"

# Pending OAuth round-trips and finished-but-uncollected logins. Both are per-process dicts with a
# short TTL, and the single-worker caveat that implies. Nothing durable belongs here: a lost entry costs one retry of the login.
_STATES: dict[str, dict] = {}
_STATE_TTL = 600        # 10 min to finish a consent screen
_HANDOFFS: dict[str, dict] = {}
_HANDOFF_TTL = 120      # the browser collects immediately; this is only for a slow redirect



def _evict() -> None:
    now = time.time()
    for k in [k for k, v in _STATES.items() if now - v.get("t", 0) > _STATE_TTL]:
        _STATES.pop(k, None)
    for k in [k for k, v in _HANDOFFS.items() if now - v.get("t", 0) > _HANDOFF_TTL]:
        _HANDOFFS.pop(k, None)


def _setting(key: str, default: str = "") -> str:
    from app.services import settings_store
    try:
        v = settings_store.get(key)
    except Exception:
        v = None
    return (v if v is not None else default) or default


def _on(key: str) -> bool:
    return str(_setting(key, "false")).strip().lower() in ("1", "true", "yes", "on")


def _base_url(request: Request) -> str:
    """The origin as the OUTSIDE world sees it — which is NOT what uvicorn sees.

    TLS is terminated at the reverse proxy, so `request.base_url` reads `http://…` (the same trap
    streams.py:_public_origin and files.py already document). Here it would be fatal rather than
    cosmetic: this string becomes the OAuth `redirect_uri`, which both providers match EXACTLY
    against the registered one and echo back at the token exchange. Google rejects plain http for a
    Web-application client outright, so every sign-in would end at redirect_uri_mismatch.
    Trust the proxy's X-Forwarded-* (only this app is exposed through it), falling back to what the
    request itself claims, and finally to https — never to http.
    """
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    proto = proto or request.url.scheme or "https"
    host = host or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}".rstrip("/")


def _error_page(msg: str, status: int = 400) -> HTMLResponse:
    return HTMLResponse(
        "<html><head><meta name='viewport' content='width=device-width,initial-scale=1'><style>"
        "body{font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;"
        "min-height:100vh;margin:0;background:#0a0612;color:#eee}div{text-align:center;max-width:34em;"
        "padding:24px}h2{color:#ff4d6d}a{color:#4de0ff}</style></head><body><div>"
        f"<h2>Sign-in failed</h2><p>{html.escape(msg)}</p>"
        "<p><a href='/client'>← back to PosterChan</a></p></div></body></html>",
        status_code=status,
    )


# --- identity -----------------------------------------------------------------------------------

def _mint_keypair() -> tuple[str, str]:
    """A fresh (nsec, npub). Rejects the astronomically-unlikely invalid scalar rather than storing
    a key that can't sign."""
    from app.services.nostr import bech32, nostr_service
    for _ in range(8):
        sk = secrets.token_bytes(32)
        try:
            pk = nostr_service.derive_pubkey(sk)
        except Exception:
            continue
        return bech32.encode("nsec", sk), nostr_service.npub_of(pk)
    raise RuntimeError("could not generate a keypair")


def _unique_username(db: Session, base: str) -> str:
    base = ("".join(c for c in (base or "user") if c.isalnum() or c in "._-") or "user")[:24]
    name = base
    for i in range(2, 200):
        if not db.query(User).filter(User.username == name).first():
            return name
        name = f"{base}{i}"
    return f"{base}{secrets.token_hex(3)}"


async def _ensure_identity(db: Session, user: User) -> str:
    """Return this user's nsec, minting and storing one if the account has no key yet, and make sure
    the key is admitted to the relay's web of trust.

    The WoT admission runs on EVERY sign-in, not only when a key is first minted. The relay is
    WoT-only, so a key that isn't in it can post nowhere and receive no DMs — and the admission is a
    best-effort call that only WARNS on failure. Doing it once, at mint time, meant any user whose
    admission failed that day (or who was dropped by a later WoT rebuild) was permanently silenced
    with nothing in the UI to explain it and no way to retry. Signing in again now repairs it, which
    is what `follow_and_admit`'s own docstring already claims it is for. Both are idempotent: the WoT
    add is a set insert and the operator follow short-circuits on "already followed".
    """
    minted = None
    if not user.nostr_nsec:
        nsec, npub = _mint_keypair()
        user.nostr_nsec = nsec
        user.nostr_npub = npub
        user.nostr_enabled = True
        db.commit()
        minted = npub
    try:
        from app.services.nostr import nostr_service
        from app.routers.client import follow_and_admit
        await follow_and_admit(db, nostr_service.to_pubkey_hex(user.nostr_npub))
    except Exception as e:
        logger.warning("[social-login] follow/admit failed for %s: %s", (user.nostr_npub or "")[:16], e)
    try:
        from app.services import nostr_store
        nostr_store.user_storage_seckey(db, user)   # get-or-create
    except Exception as e:
        logger.warning("[social-login] storage key provisioning failed: %s", e)
    if minted:
        logger.info("[social-login] minted a key for %s (%s)", user.username, minted[:16])
    return user.nostr_nsec


def _handoff(user: User, provider: str, detail: str, created: bool) -> str:
    _evict()
    code = secrets.token_urlsafe(32)
    _HANDOFFS[code] = {"t": time.time(), "user_id": user.id, "nsec": user.nostr_nsec,
                       "npub": user.nostr_npub, "provider": provider, "detail": detail,
                       "created": bool(created)}
    return code


class HandoffRequest(BaseModel):
    code: str


@router.post("/handoff")
def collect_handoff(data: HandoffRequest):
    """Exchange the one-time code from the redirect for the account's key. Single use."""
    _evict()
    entry = _HANDOFFS.pop((data.code or "").strip(), None)
    if not entry:
        raise HTTPException(status_code=404, detail="that sign-in link has expired — please try again")
    return {"nsec": entry["nsec"], "npub": entry["npub"], "provider": entry["provider"],
            "account": entry["detail"], "created": entry["created"]}


@router.get("/providers")
def providers():
    """What the login screen should offer. Public — it is read before anyone is signed in — and it
    takes no DB session: every value comes from the settings cache, and this is hit on every cold
    load of the login page."""
    return {
        "google": bool(_on("google_login_enabled") and _setting("google_client_id")
                       and _setting("google_client_secret")),
    }


# --- Google -------------------------------------------------------------------------------------

@router.get("/google/start")
def google_start(request: Request):
    if not _on("google_login_enabled"):
        return _error_page("Google sign-in is not enabled on this server.")
    client_id = _setting("google_client_id")
    if not client_id or not _setting("google_client_secret"):
        return _error_page("Google sign-in is not configured on this server.")
    _evict()
    state = secrets.token_urlsafe(24)
    redirect_uri = f"{_base_url(request)}/api/auth/google/callback"
    _STATES[state] = {"t": time.time(), "p": "google", "redirect_uri": redirect_uri}
    q = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        # Ask every time rather than silently reusing a session: this mints an identity on first use,
        # so "which Google account is this?" must be a decision the person actually makes.
        "prompt": "select_account",
    })
    return RedirectResponse(f"{GOOGLE_AUTH}?{q}", status_code=302)


async def _google_identity(code: str, redirect_uri: str) -> dict:
    """Code → {sub, email, name}. The token exchange happens server-to-server with the client secret."""
    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(GOOGLE_TOKEN, data={
            "code": code,
            "client_id": _setting("google_client_id"),
            "client_secret": _setting("google_client_secret"),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        })
        if r.status_code != 200:
            raise RuntimeError(f"token exchange failed ({r.status_code})")
        tok = r.json().get("access_token")
        if not tok:
            raise RuntimeError("no access token returned")
        # userinfo over the freshly-issued access token, rather than decoding the id_token locally:
        # the response comes straight from Google over TLS, so there is no signature for us to get
        # wrong, and no JWT library in the dependency list for one call.
        u = await c.get(GOOGLE_USERINFO, headers={"Authorization": f"Bearer {tok}"})
        if u.status_code != 200:
            raise RuntimeError(f"could not read the Google profile ({u.status_code})")
    info = u.json()
    if not info.get("sub"):
        raise RuntimeError("Google returned no account id")
    return info


@router.get("/google/callback")
async def google_callback(request: Request, code: str = None, state: str = None,
                          error: str = None, db: Session = Depends(get_db)):
    if error:
        return _error_page(f"Google returned: {error}")
    if not code or not state:
        return _error_page("Missing code or state.")
    _evict()
    pending = _STATES.pop(state, None)
    if not pending or pending.get("p") not in ("google", "google-link"):
        return _error_page("That sign-in took too long — please try again.")
    try:
        info = await _google_identity(code, pending["redirect_uri"])
    except Exception as e:
        logger.warning("[social-login] google exchange failed: %s", e)
        return _error_page(str(e))

    sub = str(info["sub"])
    email = (info.get("email") or "").strip()[:320]

    # Linking Google to a key the user already has: the account is known, Google only says who is
    # attaching. No handoff code — they are already signed in in the tab that started this.
    if pending["p"] == "google-link":
        user = db.query(User).filter(User.id == pending["user_id"]).first()
        if not user:
            return _error_page("That account no longer exists.")
        taken = db.query(User).filter(User.google_sub == sub, User.id != user.id).first()
        if taken:
            return _error_page("That Google account is already linked to a different identity here.")
        user.google_sub = sub
        user.google_email = email or None
        if pending.get("nsec"):
            user.nostr_nsec = pending["nsec"]
            user.nostr_enabled = True
        db.commit()
        logger.info("[social-login] google linked to %s", user.username)
        return RedirectResponse("/client?linked=google", status_code=302)

    user = db.query(User).filter(User.google_sub == sub).first()
    created = False
    if not user:
        from app.services import registration_service
        if not registration_service.enabled():
            return _error_page(registration_service.closed_message(), 403)
        # Deliberately NOT matched on email: addresses are re-assignable and a match would hand an
        # existing identity to whoever holds the address today. A user who wants their existing key
        # reachable by Google links it themselves (see /google/link).
        user = User(
            username=_unique_username(db, (email.split("@")[0] if email else "google")),
            email=None, password_hash="",
            is_admin=False, email_verified=True,
            google_sub=sub, google_email=email or None,
            can_image=True, can_music=True, can_video=False, can_torrent=False,
            can_blossom=False, can_ai=False,   # gated, same as a Nostr signup — an admin grants AI
        )
        from app.auth import get_password_hash
        user.password_hash = get_password_hash(secrets.token_urlsafe(32))   # unusable: OAuth-only
        db.add(user)
        db.commit()
        db.refresh(user)
        created = True
        logger.info("[social-login] google signup: %s", user.username)
    elif email and user.google_email != email:
        user.google_email = email
        db.commit()

    await _ensure_identity(db, user)
    return RedirectResponse(f"/client?login={_handoff(user, 'google', email or 'Google', created)}",
                            status_code=302)


# --- linking Google to a key you already have ---------------------------------------------------

class GoogleLinkStart(BaseModel):
    pubkey: str
    auth: str
    nsec: str = ""


@router.post("/google/link/start")
def google_link_start(data: GoogleLinkStart, request: Request, db: Session = Depends(get_db)):
    """Begin attaching Google to an EXISTING key (User Settings), so it can sign in with Google later.

    This is the one place a user-held secret key is uploaded, so it is authenticated with that very
    key (the self-proof used by every other /client write) and the key is held only for the length of
    the round-trip — it is stored on the account when Google confirms who is linking.
    """
    if not _on("google_login_enabled"):
        raise HTTPException(status_code=403, detail="Google sign-in is not enabled on this server")
    from app.routers.client import _verify_self_auth
    from app.services.nostr import nostr_service
    pk = nostr_service.to_pubkey_hex(data.pubkey or "")
    if not pk or not _verify_self_auth(data.auth, pk):
        raise HTTPException(status_code=401, detail="bad auth")
    npub = nostr_service.npub_of(pk)
    user = db.query(User).filter(User.nostr_npub == npub).first()
    if not user:
        raise HTTPException(status_code=404, detail="sign in on this server first")
    nsec = (data.nsec or "").strip()
    if nsec:
        try:
            if nostr_service.npub_from_seckey(nsec) != npub:
                raise HTTPException(status_code=400, detail="that key is not this account's key")
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(status_code=400, detail="that doesn't look like a valid nsec")
    elif not user.nostr_nsec:
        # Without a stored key, Google could identify them but never sign them in on a new device —
        # say so instead of linking something that can't do what they asked for.
        raise HTTPException(status_code=400,
                            detail="send your nsec to link it, or Google sign-in can't restore this account")
    _evict()
    state = secrets.token_urlsafe(24)
    _STATES[state] = {"t": time.time(), "p": "google-link", "user_id": user.id, "nsec": nsec,
                      "redirect_uri": f"{_base_url(request)}/api/auth/google/callback"}
    q = urllib.parse.urlencode({
        "client_id": _setting("google_client_id"),
        "redirect_uri": _STATES[state]["redirect_uri"],
        "response_type": "code", "scope": "openid email profile",
        "state": state, "prompt": "select_account",
    })
    return {"auth_url": f"{GOOGLE_AUTH}?{q}"}
