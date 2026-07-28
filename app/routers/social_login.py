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
# short TTL — the same pattern (and the same single-worker caveat) as the pending-state map in
# routers/pleroma.py. Nothing durable belongs here: a lost entry costs one retry of the login.
_STATES: dict[str, dict] = {}
_STATE_TTL = 600        # 10 min to finish a consent screen
_HANDOFFS: dict[str, dict] = {}
_HANDOFF_TTL = 120      # the browser collects immediately; this is only for a slow redirect

# How many already-linked accounts a Pleroma login will probe to backfill their handle (see
# _find_pleroma_user). Bounded so one login can't turn into hundreds of calls to an instance.
_ACCT_BACKFILL_LIMIT = 25


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
    """Return this user's nsec, minting and storing one if the account has no key yet.

    Also admits a brand-new key to the relay's web of trust, the same as a browser-side signup —
    without it the account can post nowhere and receive no DMs.
    """
    if user.nostr_nsec:
        return user.nostr_nsec
    nsec, npub = _mint_keypair()
    user.nostr_nsec = nsec
    user.nostr_npub = npub
    user.nostr_enabled = True
    db.commit()
    try:
        from app.services.nostr import nostr_service
        from app.routers.client import follow_and_admit
        await follow_and_admit(db, nostr_service.to_pubkey_hex(npub))
    except Exception as e:
        logger.warning("[social-login] follow/admit failed for %s: %s", npub[:16], e)
    try:
        from app.services import nostr_store
        nostr_store.user_storage_seckey(db, user)
    except Exception as e:
        logger.warning("[social-login] storage key provisioning failed: %s", e)
    return nsec


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
        "pleroma": _on("pleroma_login_enabled"),
        "pleroma_instance": _setting("pleroma_login_instance") or _setting("fedi_bridge_instance_url"),
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


# --- Pleroma / Mastodon -------------------------------------------------------------------------

def _acct_of(account: dict, instance_url: str) -> str:
    """`user@host` for an account as its own instance reports it (`acct` is bare there)."""
    acct = (account.get("acct") or account.get("username") or "").strip()
    if not acct:
        return ""
    if "@" not in acct:
        acct = f"{acct}@{urllib.parse.urlparse(instance_url).hostname or ''}"
    return acct.lower()[:255]


async def _find_pleroma_user(db: Session, instance_url: str, acct: str) -> User | None:
    """The User this fediverse account already belongs to, if any.

    Straight match on the recorded handle first. Accounts linked BEFORE pleroma_acct existed have
    none recorded, so those get backfilled here — ask the instance who each stored token belongs to,
    bounded, once per account. Without this every existing bridge user signing in with Pleroma would
    be handed a brand-new empty identity instead of their own.
    """
    hit = db.query(User).filter(User.pleroma_instance_url == instance_url,
                                User.pleroma_acct == acct).first()
    if hit:
        return hit
    from app.services.pleroma_service import verify_credentials
    stale = (db.query(User)
             .filter(User.pleroma_instance_url == instance_url,
                     User.pleroma_access_token.isnot(None),
                     User.pleroma_acct.is_(None))
             .limit(_ACCT_BACKFILL_LIMIT + 1).all())
    if len(stale) > _ACCT_BACKFILL_LIMIT:
        logger.info("[social-login] %d unlabelled pleroma links on %s — probing the first %d",
                    len(stale), instance_url, _ACCT_BACKFILL_LIMIT)
        stale = stale[:_ACCT_BACKFILL_LIMIT]
    async def _who(u):
        try:
            return u, _acct_of(await verify_credentials(instance_url, u.pleroma_access_token), instance_url)
        except Exception:
            return u, ""     # revoked/expired token — leave it unlabelled, it just isn't a match

    # CONCURRENTLY: this runs inside someone's login. Sequentially, 25 probes against a slow instance
    # is 25 round trips stacked end to end — long enough that the person gives up and retries, which
    # starts the whole thing again.
    import asyncio
    found = None
    for u, got in await asyncio.gather(*[_who(u) for u in stale]):
        if not got:
            continue
        u.pleroma_acct = got
        if got == acct:
            found = u
    db.commit()
    return found


class PleromaLoginStart(BaseModel):
    instance_url: str = ""


@router.post("/pleroma/start")
async def pleroma_start(data: PleromaLoginStart, request: Request):
    """Register this app on the instance (public /api/v1/apps — no admin anything) and return the
    consent URL. Read-only scope: signing in is not permission to post as you."""
    if not _on("pleroma_login_enabled"):
        raise HTTPException(status_code=403, detail="fediverse sign-in is not enabled on this server")
    instance = (data.instance_url or "").strip().rstrip("/")
    if not instance:
        instance = (_setting("pleroma_login_instance") or _setting("fedi_bridge_instance_url")).rstrip("/")
    if not instance.startswith(("http://", "https://")):
        instance = "https://" + instance if instance else ""
    if not instance:
        raise HTTPException(status_code=400, detail="which instance?")
    # Same SSRF guard the bridge puts on instance URLs — this one is typed in by an anonymous visitor.
    try:
        from app.services.rss_service import is_safe_host, looks_fetchable
        if not looks_fetchable(instance) or not is_safe_host(instance):
            raise HTTPException(status_code=400, detail="that instance address isn't allowed")
    except HTTPException:
        raise
    except Exception:
        pass
    from app.services.pleroma_service import register_app, build_auth_url
    redirect_uri = f"{_base_url(request)}/api/auth/pleroma/callback"
    try:
        app_data = await register_app(instance, redirect_uri, scopes="read")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"could not reach that instance: {e}")
    cid, csec = app_data.get("client_id"), app_data.get("client_secret")
    if not cid or not csec:
        raise HTTPException(status_code=502, detail="that instance did not return client credentials")
    _evict()
    state = secrets.token_urlsafe(24)
    _STATES[state] = {"t": time.time(), "p": "pleroma", "instance": instance,
                      "client_id": cid, "client_secret": csec, "redirect_uri": redirect_uri}
    return {"auth_url": build_auth_url(instance, cid, redirect_uri, scopes="read") + f"&state={state}"}


@router.get("/pleroma/callback")
async def pleroma_callback(code: str = None, state: str = None, error: str = None,
                           db: Session = Depends(get_db)):
    if error:
        return _error_page(f"The instance returned: {error}")
    if not code or not state:
        return _error_page("Missing code or state.")
    _evict()
    pending = _STATES.pop(state, None)
    if not pending or pending.get("p") != "pleroma":
        return _error_page("That sign-in took too long — please try again.")
    from app.services.pleroma_service import exchange_code, verify_credentials
    instance = pending["instance"]
    try:
        token = await exchange_code(instance_url=instance, client_id=pending["client_id"],
                                    client_secret=pending["client_secret"],
                                    redirect_uri=pending["redirect_uri"], code=code)
        account = await verify_credentials(instance, token)
    except Exception as e:
        logger.warning("[social-login] pleroma exchange failed: %s", e)
        return _error_page(f"Sign-in failed: {e}")
    acct = _acct_of(account, instance)
    if not acct:
        return _error_page("That instance did not say who you are.")

    user = await _find_pleroma_user(db, instance, acct)
    created = False
    if not user:
        user = User(
            username=_unique_username(db, acct.split("@")[0]), email=None, password_hash="",
            is_admin=False, email_verified=True,
            can_image=True, can_music=True, can_video=False, can_torrent=False,
            can_blossom=False, can_ai=False,
        )
        from app.auth import get_password_hash
        user.password_hash = get_password_hash(secrets.token_urlsafe(32))
        db.add(user)
        db.commit()
        db.refresh(user)
        created = True
        logger.info("[social-login] pleroma signup: %s (%s)", user.username, acct)
    # Signing in IS the link: fill in the Social settings this account would otherwise have to be
    # configured by hand, so a fresh account comes back already connected to its instance.
    user.pleroma_instance_url = instance
    user.pleroma_acct = acct
    # …but do NOT overwrite a token that is already there. This flow asks for `read` only — signing in
    # is not permission to post as you — whereas the account-linking flow in routers/pleroma.py asks
    # for `read write follow`, which is what the bridge's write-back (replying/liking from Nostr) runs
    # on. Refreshing the token here would quietly downgrade an existing bridge user to read-only the
    # first time they signed in with Pleroma, and the failure would surface much later as "my replies
    # stopped reaching the fediverse".
    if not user.pleroma_access_token:
        user.pleroma_access_token = token
        user.pleroma_enabled = True
    db.commit()
    await _ensure_identity(db, user)
    return RedirectResponse(f"/client?login={_handoff(user, 'pleroma', acct, created)}", status_code=302)


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
