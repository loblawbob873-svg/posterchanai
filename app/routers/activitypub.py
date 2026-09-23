"""ActivityPub HTTP surface. Thin: the work is in app/services/activitypub/.

Everything answers 404 while `activitypub_enabled` is off, so a node that never turns it on looks
exactly like one that never had it. Paths live under `/ap/` because router.lan's nginx answers 410
for the retired Pleroma's `/inbox` and `/users/*/inbox` -- those rules stay, and these do not
collide with them.
"""
from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.auth import get_admin_user, get_current_user
from app.services.activitypub import actors, config, convert, httpsig, inbox, remote, state

logger = logging.getLogger(__name__)
router = APIRouter(tags=["activitypub"])

MAX_BODY = 1024 * 1024
_JRD = "application/jrd+json; charset=utf-8"


def _on() -> None:
    if not config.enabled() or not config.base_url():
        raise HTTPException(404, "Not Found")


def _ap(doc: dict, status: int = 200) -> Response:
    return Response(json.dumps(doc, separators=(",", ":")), status_code=status,
                    media_type=config.AP_CONTENT_TYPE, headers={"Cache-Control": "max-age=60"})


def _wants_html(request: Request) -> bool:
    accept = (request.headers.get("accept") or "").lower()
    return "text/html" in accept and "activity+json" not in accept and "ld+json" not in accept


# ------------------------------------------------------------------------------------ discovery

@router.get("/.well-known/webfinger")
async def webfinger(resource: str = ""):
    _on()
    res = resource.strip()
    base, dom = config.base_url(), config.domain()
    name = ""
    if res.lower().startswith("acct:"):
        user, _, host = res[5:].lstrip("@").partition("@")
        if host.lower() == dom:
            name = user
    elif res.startswith(f"{base}/ap/users/"):
        name = res[len(f"{base}/ap/users/"):].split("/")[0]
    if name == dom or res in (f"{base}/ap/actor", f"acct:{dom}@{dom}"):
        return JSONResponse({"subject": f"acct:{dom}@{dom}", "links": [
            {"rel": "self", "type": config.AP_CONTENT_TYPE, "href": f"{base}/ap/actor"}]}, media_type=_JRD)
    pk = await actors.member_by_name(name) if name else ""
    if not pk:
        raise HTTPException(404, "Not Found")
    name = actors.handle(pk)
    actor = convert.actor_url(base, name)
    return JSONResponse({"subject": f"acct:{name}@{dom}", "aliases": [actor, f"{base}/users/{name}"],
                         "links": [{"rel": "self", "type": config.AP_CONTENT_TYPE, "href": actor},
                                   {"rel": "http://webfinger.net/rel/profile-page", "type": "text/html",
                                    "href": f"{base}/users/{name}"}]}, media_type=_JRD)


@router.get("/.well-known/nodeinfo")
async def nodeinfo_links():
    _on()
    return JSONResponse({"links": [{"rel": "http://nodeinfo.diaspora.software/ns/schema/2.1",
                                    "href": f"{config.base_url()}/nodeinfo/2.1"}]})


@router.get("/nodeinfo/2.1")
async def nodeinfo():
    _on()
    users = len(actors.all_actors())
    return JSONResponse({
        "version": "2.1",
        "software": {"name": "posterchan", "version": "1",
                     "repository": "https://github.com/loblawbob873-svg/posterchanai"},
        "protocols": ["activitypub"],
        "services": {"inbound": [], "outbound": []},
        "openRegistrations": False,
        "usage": {"users": {"total": users}},
        "metadata": {"nodeName": config.domain(), "nostr": True},
    })


# ------------------------------------------------------------------------------------ actors

@router.get("/ap/actor")
async def instance_actor():
    _on()
    return _ap(await actors.instance_actor())


async def _member(name: str) -> str:
    pk = await actors.member_by_name(name)
    if not pk:
        raise HTTPException(404, "Not Found")
    return pk


@router.get("/ap/users/{name}")
async def actor(name: str, request: Request):
    _on()
    pk = await _member(name)
    if _wants_html(request):
        return RedirectResponse(f"{config.base_url()}/users/{actors.handle(pk)}", status_code=302)
    try:
        return _ap(await actors.person(actors.handle(pk), pk, anonymous=True))
    except state.MintLimited:
        # A burst of first requests for accounts that are not local users: ask again shortly.
        return Response(status_code=503, headers={"Retry-After": "60"})


def _collection(url: str, total: int) -> dict:
    return {"@context": "https://www.w3.org/ns/activitystreams", "id": url,
            "type": "OrderedCollection", "totalItems": total}


@router.get("/ap/users/{name}/followers")
async def followers(name: str):
    _on()
    pk = await _member(name)
    url = f"{convert.actor_url(config.base_url(), actors.handle(pk))}/followers"
    return _ap(_collection(url, await state.follower_count(pk)))


@router.get("/ap/users/{name}/following")
async def following(name: str):
    _on()
    pk = await _member(name)
    url = f"{convert.actor_url(config.base_url(), actors.handle(pk))}/following"
    try:
        n = len(await state.following(pk, strict=False))
    except Exception:
        n = 0
    return _ap(_collection(url, n))


@router.get("/ap/users/{name}/outbox")
async def outbox(name: str):
    """The count only. The posts themselves are on Nostr; a remote server that wants one fetches it
    by its object URL."""
    _on()
    pk = await _member(name)
    return _ap(_collection(f"{convert.actor_url(config.base_url(), actors.handle(pk))}/outbox", 0))


@router.get("/ap/objects/{event_id}")
async def note(event_id: str, request: Request):
    """A member's post, straight from the relay. Anything that is not a member's own public note
    (a mirror, somebody else's event, a DM) is 404 -- this is not a window onto the relay."""
    _on()
    from app.services.activitypub import outbox as ob
    ev = await ob._event(event_id) if len(event_id) == 64 else None
    name = actors.handle(ev.get("pubkey", "")) if ev else ""
    if not ev or not name or ev.get("kind") not in (1, 1111) or ob._is_mirror(ev) or ob._protected(ev):
        raise HTTPException(404, "Not Found")
    if not await actors.exposed(ev["pubkey"]):
        raise HTTPException(404, "Not Found")
    if _wants_html(request):
        return RedirectResponse(f"{config.base_url()}/{convert._nevent_or_note(event_id)}", status_code=302)
    base = config.base_url()
    me = convert.actor_url(base, name)
    parent = ob.parent_of(ev)
    target = await ob.resolve_event(parent) if parent else {}
    mentions = {}
    for pk in ob._referenced_pubkeys(ev):
        who = await ob.resolve_pubkey(pk)
        if who:
            mentions[pk] = who
    doc = convert.note_from_event(ev, base=base, actor=me, followers=f"{me}/followers", mentions=mentions,
                                  in_reply_to=target.get("uri", ""), reply_to_actor=target.get("actor", ""))
    doc["@context"] = convert.AS_CONTEXT
    return _ap(doc)


# ------------------------------------------------------------------------------------ inbox

async def _verified(request: Request) -> tuple[dict, str]:
    """(activity, signer actor id), or raise. The signature is checked against the PUBLIC host:
    behind router.lan the upstream request may carry another Host header than the one signed."""
    body = await request.body()
    if len(body) > MAX_BODY:
        raise HTTPException(413, "Too large")
    try:
        activity = json.loads(body)
    except ValueError:
        raise HTTPException(400, "Not JSON")
    if not isinstance(activity, dict) or not activity.get("type"):
        raise HTTPException(400, "Not an activity")
    try:
        params = httpsig.parse(request.headers.get("signature") or "")
    except httpsig.SignatureError:
        raise HTTPException(401, "Unsigned")
    headers = {k.lower(): v for k, v in request.headers.items()}
    headers["host"] = config.domain()
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    last = None
    now = time.monotonic()
    # A rotated key is fetched again -- ONCE per key per ten minutes. Unthrottled, every badly
    # signed request would make us re-fetch a third party's actor with our instance key: an
    # amplifier anyone could aim at anyone.
    may_refresh = now - _refreshed.get(params["keyId"], -1e9) > 600
    for refresh in ((False, True) if may_refresh else (False,)):
        if refresh:
            _refreshed[params["keyId"]] = now
            if len(_refreshed) > 5000:
                _refreshed.clear()
        try:
            owner, pem = await remote.public_key(params["keyId"], refresh=refresh)
            httpsig.verify(request.method, path, headers, body, pem, params)
            return activity, owner
        except (remote.FetchError, httpsig.SignatureError) as e:
            last = e
    # An account that has been DELETED can no longer be fetched, so its own Delete can never
    # verify; answering 401 makes its server retry that forever. It is gone either way.
    if activity.get("type") == "Delete" and isinstance(last, remote.FetchError):
        raise HTTPException(202, "Accepted")
    raise HTTPException(401, f"Signature not accepted: {last}")


_refreshed: dict = {}            # keyId -> when it was last force-refreshed
_hits: dict = {}                 # remote host -> (window start, count)
PER_HOST_PER_MINUTE = 300


def _rate_ok(host: str) -> bool:
    """A per-server budget: one instance cannot make this node mint puppets and fetch actors
    without limit. Generous -- a busy server sends a burst after every popular post."""
    now = time.monotonic()
    start, n = _hits.get(host, (now, 0))
    if now - start > 60:
        start, n = now, 0
    _hits[host] = (start, n + 1)
    if len(_hits) > 10000:
        _hits.clear()
    return n < PER_HOST_PER_MINUTE


async def _receive(request: Request) -> Response:
    _on()
    peer = (request.headers.get("signature") or "")
    try:
        from app.services.activitypub.httpsig import parse
        peer_host = remote.host_of(parse(peer)["keyId"])
    except Exception:
        peer_host = ""
    if peer_host and not _rate_ok(peer_host):
        return Response(status_code=429, headers={"Retry-After": "60"})
    try:
        activity, signer = await _verified(request)
    except HTTPException as e:
        if e.status_code == 202:
            return Response(status_code=202)
        raise
    if convert.id_of(activity.get("actor")) != signer:
        raise HTTPException(401, "Signed by somebody other than the actor")
    host = remote.host_of(signer)
    if config.is_own_host(host) or config.host_blocked(host):
        return Response(status_code=202)          # blocked instances: accepted and dropped, so they stop
    if not inbox.schedule(activity, signer):
        return Response(status_code=503, headers={"Retry-After": "120"})
    return Response(status_code=202)


@router.post("/ap/inbox")
async def shared_inbox(request: Request):
    return await _receive(request)


@router.post("/ap/users/{name}/inbox")
async def user_inbox(name: str, request: Request):
    return await _receive(request)


# ------------------------------------------------------------------------------------ app API

@router.get("/api/activitypub/lookup")
async def lookup(acct: str, user=Depends(get_current_user)):
    """`user@host` (or an actor URL) → that fediverse account's Nostr identity on this node, so the
    client can open and follow it. Following it in the client adds the puppet to the contact list;
    the delivery loop turns that into an ActivityPub Follow."""
    _on()
    acct = (acct or "").strip().lstrip("@")
    try:
        uri = acct if acct.startswith("https://") else await remote.webfinger(acct)
        doc = await remote.actor(uri)
    except remote.FetchError as e:
        raise HTTPException(404, str(e))
    p = await inbox._puppet(doc)
    if not p:
        raise HTTPException(404, "That account could not be mirrored")
    return {"pubkey": p["pubkey_hex"], "npub": p["npub"], "acct": p.get("acct", ""), "actor": convert.id_of(doc)}


@router.post("/api/activitypub/import-following")
async def import_following(user=Depends(get_current_user)):
    """The member's linked Pleroma/Mastodon following list, as puppet pubkeys for the client to add
    to its contact list (see importer.py)."""
    _on()
    if not (getattr(user, "pleroma_instance_url", "") and getattr(user, "pleroma_access_token", "")):
        raise HTTPException(400, "Connect your Pleroma/Mastodon account in Settings first")
    from app.database import SessionLocal
    from app.services.activitypub import importer
    try:
        accounts = await importer.following(user.pleroma_instance_url, user.pleroma_access_token)
    except Exception as e:
        raise HTTPException(502, f"Could not read who you follow there: {type(e).__name__}")
    db = SessionLocal()
    try:
        people = await importer.puppets_for(db, accounts, user.pleroma_instance_url)
    finally:
        db.close()
    return {"following": len(accounts), "people": people}


@router.get("/api/admin/activitypub/status")
async def admin_status(user=Depends(get_admin_user)):
    """What Admin → Fediverse (ActivityPub) shows: whether it is on, the address people use, who
    federates, and the delivery loop's last report (written by the worker)."""
    from app.services import nostr_store, settings_store
    base = config.base_url()
    by_name, by_pk = actors._registry()
    rows = []
    if config.enabled() and base:
        for pk, name in sorted(by_pk.items(), key=lambda x: x[1])[:500]:
            member = actors.is_actor(pk)
            rows.append({"name": name, "handle": f"@{name}@{config.domain()}", "member": member,
                         "via_linked_account": actors.uses_linked_account(pk),
                         "followers": await state.follower_count(pk) if member else 0})
    try:
        report = await nostr_store.get_doc(settings_store._port(), "pcai:ap:stats",
                                           seckey=settings_store._operator_seckey(None)) or {}
    except Exception:
        report = {}
    return {"enabled": config.enabled(), "domain": config.domain(), "base": base,
            "webfinger": f"{base}/.well-known/webfinger?resource=acct:NAME@{config.domain()}" if base else "",
            "blocked_instances": sorted(config.blocked_domains()), "members": rows, "delivery": report}
