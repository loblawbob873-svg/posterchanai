"""GRASP git host — provisioning + repo CRUD + announce API (P2).

Thin router; all logic in app/services/git_host_service.py. Every route is HARD-GATED on
`git_server_enabled` (404 when off) — the hard safety constraint: the feature is inert until an
admin turns it on. Provisioning is gated to admins ∪ the `git_server_allowlist` npubs (matching the
node_exec abuse-bounding style).

Private repos: NOT announced (no public 30617/30618). The listing endpoint is admin-gated so private
repos aren't disclosed anonymously.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from app.auth import get_current_user
from app.models import User
from app.services import settings_store, git_host_service as ghs, git_proxy

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/git", tags=["git"])

# Smart-HTTP reverse-proxy router (NO /api prefix): mounted at /git/ (matching the recommended nginx
# `location /git/`). Active ONLY when git_server_proxy_url is set — on a hosting node these paths are
# served by the git_host_main subprocess via nginx→3053, so the app never sees them. Registered before
# the client SPA catch-all in app/main.py so /git/… wins.
smart_router = APIRouter(tags=["git"])


@smart_router.api_route("/git/{repo_path:path}", methods=["GET", "POST", "OPTIONS"])
async def git_smart_proxy(repo_path: str, request: Request):
    """Thin reverse-proxy of a git smart-HTTP request to the hosting node (git_server_proxy_url).
    404 when this node isn't a proxy (empty git_server_proxy_url). No auth here — the hosting node
    authorizes the forwarded NIP-98/30618, exactly like the Blossom storage proxy."""
    if not git_proxy.proxy_enabled():
        raise HTTPException(status_code=404, detail="not a git proxy node")
    # Answer the CORS preflight HERE rather than forwarding it: an in-browser git client sends
    # OPTIONS before any request carrying Authorization/Git-Protocol, and the git host speaks only
    # GET/POST — the preflight used to fall through to a 400, which reads to the browser as "this
    # origin may not talk to that server" and blocks the real request that would have succeeded.
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=git_proxy.cors_headers())
    return await git_proxy.proxy_git_request(request, repo_path)


def _enabled() -> bool:
    return settings_store.get_bool("git_server_enabled", False)


def _require_enabled():
    if not _enabled():
        # 404 (not 403) so the feature is indistinguishable from absent when off.
        raise HTTPException(status_code=404, detail="git host not enabled")


def _require_local_host():
    """Provisioning/announce/list must run on the HOSTING node (where the repos + hooks + DB live).
    A proxy node forwards smart-HTTP but has no local repos, so these management calls are refused
    there — provision on the node that actually hosts (git_server_proxy_url empty)."""
    if git_proxy.proxy_enabled():
        raise HTTPException(status_code=400,
                            detail="this node proxies git to %s — provision/manage on the hosting node"
                                   % settings_store.get("git_server_proxy_url", ""))


def _may_provision(user: User) -> bool:
    """Admins always; otherwise the user's npub/hex must be on git_server_allowlist."""
    if getattr(user, "is_admin", False):
        return True
    allow = settings_store.get("git_server_allowlist", "") or ""
    from app.services.nostr import nostr_service
    mine = set()
    for attr in ("nostr_npub", "nostr_pubkey"):
        v = getattr(user, attr, None)
        if v:
            h = nostr_service.to_pubkey_hex(v)
            if h:
                mine.add(h)
    for tok in allow.replace(",", "\n").split():
        h = nostr_service.to_pubkey_hex(tok.strip())
        if h and h in mine:
            return True
    return False


def _owner_hex_for(user: User, body_owner: str | None) -> str | None:
    """Resolve the repo owner hex. Admins may pass an explicit owner; otherwise it's the caller."""
    from app.services.nostr import nostr_service
    if body_owner and getattr(user, "is_admin", False):
        return nostr_service.to_pubkey_hex(body_owner)
    for attr in ("nostr_npub", "nostr_pubkey"):
        v = getattr(user, attr, None)
        if v:
            h = nostr_service.to_pubkey_hex(v)
            if h:
                return h
    return None


@router.get("/status")
def status(user: User = Depends(get_current_user)):
    _require_enabled()
    if git_proxy.proxy_enabled():
        # Proxy node: no local subprocess; report the mode + upstream host.
        return {"mode": "proxy", "proxy_url": settings_store.get("git_server_proxy_url", ""),
                "running": True}
    from app.services.git_http_service import git_http_status
    st = git_http_status()
    st["mode"] = "local"
    return st


@router.get("/repos")
def list_repos(user: User = Depends(get_current_user)):
    """List hosted repos. Admin-gated so private repos are not disclosed anonymously."""
    _require_enabled()
    _require_local_host()
    if not getattr(user, "is_admin", False):
        raise HTTPException(status_code=403, detail="admin only")
    return {"repos": ghs.list_repos(), "total_gb": ghs.total_size_gb()}


@router.post("/host")
async def host_repo(request: Request, user: User = Depends(get_current_user)):
    """Create/host a bare repo. Body: {repo_id, name?, description?, owner?(admin), private?, readers?}.
    Returns the clone/web/relays URLs + the suggested 30617 tags for the client to sign+publish."""
    _require_enabled()
    _require_local_host()
    if not _may_provision(user):
        raise HTTPException(status_code=403, detail="not allowed to provision repos")
    body = await request.json()
    repo_id = ghs.sanitize_repo_id(str(body.get("repo_id", "")))
    if not repo_id:
        raise HTTPException(status_code=400, detail="invalid repo_id (need [a-z0-9._-])")
    owner_hex = _owner_hex_for(user, body.get("owner"))
    if not owner_hex:
        raise HTTPException(status_code=400, detail="no nostr key on your account to own the repo")

    private = bool(body.get("private", settings_store.get_bool("git_server_default_private", False)))
    readers = body.get("readers") or []

    res = ghs.create_repo(owner_hex, repo_id, private=private, readers=readers)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "create failed"))

    base = settings_store.get("git_server_public_base", "") or ""
    from app.services.nostr import nostr_service
    npub = nostr_service.npub_of(owner_hex)
    clone = "%s/%s/%s.git" % (base.rstrip("/"), npub, repo_id) if base else ""

    out = {"ok": True, "owner": owner_hex, "npub": npub, "repo_id": repo_id,
           "private": private, "clone": clone, "created": res.get("created")}
    if not private:
        # Suggested NIP-34 30617 tags for the CLIENT to sign+publish (public repos only).
        tags = [["d", repo_id]]
        if body.get("name"):
            tags.append(["name", str(body["name"])])
        if body.get("description"):
            tags.append(["description", str(body["description"])])
        if clone:
            tags.append(["clone", clone])
        tags.append(["maintainers", owner_hex])
        out["announce_tags_30617"] = tags
    else:
        out["note"] = "private repo — not announced; clone requires a NIP-98 Authorization header"
    return out


@router.post("/announce")
async def announce_repo(request: Request, user: User = Depends(get_current_user)):
    """Publish 30617 (+ an initial 30618 from current refs) for a PUBLIC repo, signed by the HOST
    OPERATOR key, to the local relay. Only valid when the repo owner IS the operator (e.g. the P4
    self-host of posterchanai). Refuses private repos. Non-operator owners sign client-side instead."""
    _require_enabled()
    _require_local_host()
    if not _may_provision(user):
        raise HTTPException(status_code=403, detail="not allowed")
    body = await request.json()
    repo_id = ghs.sanitize_repo_id(str(body.get("repo_id", "")))
    owner_hex = _owner_hex_for(user, body.get("owner"))
    if not repo_id or not owner_hex:
        raise HTTPException(status_code=400, detail="repo_id/owner required")
    if not ghs.repo_exists(owner_hex, repo_id):
        raise HTTPException(status_code=404, detail="repo not hosted here")
    if ghs.is_private(owner_hex, repo_id):
        raise HTTPException(status_code=400, detail="private repos are not announced")

    from app.services import keystore
    from app.services.nostr import nostr_service
    op_nsec = keystore.get_operator_nsec()
    if not op_nsec or nostr_service.derive_pubkey(nostr_service.decode_seckey(op_nsec)) != owner_hex:
        # Only the operator can be signed for server-side; return the tags for client signing.
        raise HTTPException(status_code=400,
                            detail="owner is not the host operator; sign the 30617 client-side")

    seckey = nostr_service.decode_seckey(op_nsec)
    base = settings_store.get("git_server_public_base", "") or ""
    npub = nostr_service.npub_of(owner_hex)
    clone = "%s/%s/%s.git" % (base.rstrip("/"), npub, repo_id) if base else ""

    from app.services.nostr.event import build_event
    from app.services import nostr_store
    port = settings_store.get_int("nostr_relay_port", 3052)

    a_tags = [["d", repo_id]]
    if body.get("name"):
        a_tags.append(["name", str(body["name"])])
    if body.get("description"):
        a_tags.append(["description", str(body["description"])])
    if clone:
        a_tags.append(["clone", clone])
    a_tags.append(["maintainers", owner_hex])
    ann = build_event(seckey, 30617, "", tags=a_tags)

    refs = ghs.repo_refs(owner_hex, repo_id)
    head = ghs.repo_head(owner_hex, repo_id)
    s_tags = [["d", repo_id]]
    if head:
        s_tags.append(["HEAD", "ref: " + head])
    for name, sha in sorted(refs.items()):
        s_tags.append([name, sha])
    s_tags.append(["a", "30617:%s:%s" % (owner_hex, repo_id)])
    state = build_event(seckey, 30618, "", tags=s_tags)

    ok1, m1 = await nostr_store.publish_event(port, ann, timeout=6.0)
    ok2, m2 = await nostr_store.publish_event(port, state, timeout=6.0)
    return JSONResponse({"ok": bool(ok1 and ok2), "announced": ok1, "state": ok2,
                         "clone": clone, "detail": {"30617": m1, "30618": m2}})
