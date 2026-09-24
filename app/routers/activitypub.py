"""ActivityPub HTTP surface. Thin: the work is in app/services/activitypub/.

Everything answers 404 while `activitypub_enabled` is off, so a node that never turns it on looks
exactly like one that never had it. Paths live under `/ap/` because router.lan's nginx answers 410
for the retired Pleroma's `/inbox` and `/users/*/inbox` -- those rules stay, and these do not
collide with them.
"""
from __future__ import annotations

import asyncio
import json
import re
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response

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


# The relay a strict state read could not reach -- the ten seconds after a restart while it starts, or
# an outage. That is "ask again shortly" (503 + Retry-After), never a 500: a 500 on an actor fetch
# reads as a broken account to the asking server, and on the inbox as a delivery it should back off.
_RELAY_DOWN = (OSError, TimeoutError, asyncio.TimeoutError)


def _relay_down() -> Response:
    return Response(status_code=503, headers={"Retry-After": "30"})


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
    by_path = False
    if res.startswith(f"{base}/ap/users/"):
        name, by_path = res[len(f"{base}/ap/users/"):].split("/")[0], True
    if name == dom or res in (f"{base}/ap/actor", f"acct:{dom}@{dom}"):
        return JSONResponse({"subject": f"acct:{dom}@{dom}", "links": [
            {"rel": "self", "type": config.AP_CONTENT_TYPE, "href": f"{base}/ap/actor"}]}, media_type=_JRD)
    try:
        pk = (await (actors.member_of_path(name, strict=True) if by_path
                     else actors.member_by_name(name, strict=True))) if name else ""
        if not pk:
            raise HTTPException(404, "Not Found")
        name = await actors.ap_handle(pk)
    except _RELAY_DOWN:
        return _relay_down()
    if not name:
        raise HTTPException(404, "Not Found")
    actor = convert.actor_url(base, name)
    # The SUBJECT is the handle the actor shows (preferredUsername): Mastodon checks that the two
    # agree before it displays it. Asked by npub or by readable handle, the answer is the same.
    shown = await actors.readable_handle(pk) or name
    return JSONResponse({"subject": f"acct:{shown}@{dom}", "aliases": [actor, f"{base}/users/{name}"],
                         "links": [{"rel": "self", "type": config.AP_CONTENT_TYPE, "href": actor},
                                   {"rel": "http://webfinger.net/rel/profile-page", "type": "text/html",
                                    "href": f"{base}/users/{name}"}]}, media_type=_JRD)


@router.get("/.well-known/nodeinfo")
async def nodeinfo_links():
    _on()
    base = config.base_url()
    return JSONResponse({"links": [
        {"rel": "http://nodeinfo.diaspora.software/ns/schema/2.1", "href": f"{base}/nodeinfo/2.1"},
        {"rel": "http://nodeinfo.diaspora.software/ns/schema/2.0", "href": f"{base}/nodeinfo/2.0"}]})


@router.get("/.well-known/host-meta")
async def host_meta():
    """The XRD pointer to WebFinger. Older servers and some clients ask here first."""
    _on()
    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<XRD xmlns="http://docs.oasis-open.org/ns/xri/xrd-1.0">'
           f'<Link rel="lrdd" template="{config.base_url()}/.well-known/webfinger?resource={{uri}}"/></XRD>')
    return Response(xml, media_type="application/xrd+xml")


def _nodeinfo(version: str) -> dict:
    from app.services import registration_service
    users = len(actors.all_actors())
    software = {"name": "posterchan", "version": "1"}
    if version == "2.1":
        software["repository"] = "https://github.com/loblawbob873-svg/posterchanai"
    try:
        open_reg = bool(registration_service.enabled())
    except Exception:
        open_reg = False
    return {
        "version": version,
        "software": software,
        "protocols": ["activitypub"],
        "services": {"inbound": [], "outbound": []},
        "openRegistrations": open_reg,
        # Only what is counted for certain: activity figures would need a relay scan per request.
        "usage": {"users": {"total": users}},
        "metadata": {"nodeName": config.domain(), "nostr": True},
    }


@router.get("/nodeinfo/2.1")
async def nodeinfo():
    _on()
    return JSONResponse(_nodeinfo("2.1"))


@router.get("/nodeinfo/2.0")
async def nodeinfo_20():
    _on()
    return JSONResponse(_nodeinfo("2.0"))


# ------------------------------------------------------------------------------------ actors

@router.get("/ap/actor")
async def instance_actor():
    _on()
    try:
        return _ap(await actors.instance_actor())
    except _RELAY_DOWN:
        return _relay_down()


@router.get("/ap/actor/outbox")
async def instance_outbox():
    """The instance actor posts nothing, but it advertises an outbox, and a 404 there is logged as an
    error by the servers that look."""
    _on()
    url = f"{config.base_url()}/ap/actor/outbox"
    return _ap({**_collection(url, 0), "orderedItems": []})


async def _member(name: str) -> str:
    try:
        pk = await actors.member_of_path(name, strict=True)
    except _RELAY_DOWN:
        raise HTTPException(503, "Try again shortly", headers={"Retry-After": "30"})
    if not pk:
        raise HTTPException(404, "Not Found")
    return pk


async def _url_of(pk: str) -> str:
    """The account's PINNED actor URL (see actors.ap_handle)."""
    try:
        me = await actors.actor_id(pk)
    except _RELAY_DOWN:
        raise HTTPException(503, "Try again shortly", headers={"Retry-After": "30"})
    if not me:
        raise HTTPException(404, "Not Found")
    return me


@router.get("/ap/users/{name}")
async def actor(name: str, request: Request):
    _on()
    pk = await _member(name)
    if _wants_html(request):
        return RedirectResponse(f"{config.base_url()}/users/{actors.handle(pk)}", status_code=302)
    try:
        return _ap(await actors.person(pk, anonymous=True))
    except state.MintLimited:
        # A burst of first requests for accounts that are not local users: ask again shortly.
        return Response(status_code=503, headers={"Retry-After": "60"})
    except _RELAY_DOWN:
        return _relay_down()


def _collection(url: str, total: int) -> dict:
    return {"@context": "https://www.w3.org/ns/activitystreams", "id": url,
            "type": "OrderedCollection", "totalItems": total}


@router.get("/ap/users/{name}/featured")
async def featured(name: str):
    """Pinned posts: the account's NIP-51 pin list (kind 10001), as the objects themselves -- what
    Mastodon, Akkoma and Misskey show at the top of a profile. A pin that is not a public post of this
    account (a mirror, somebody else's, deleted) is left out."""
    _on()
    pk = await _member(name)
    me = await _url_of(pk)
    from app.services.activitypub import outbox as ob
    items = []
    for eid in await actors.featured_ids(pk):
        ev = await ob._event(eid)
        if not ev or ev.get("pubkey") != pk:
            continue
        obj, _ctx = await ob.build_object(ev, me)
        if obj is not None:
            items.append(obj)
    return _ap({**_collection(f"{me}/featured", len(items)), "orderedItems": items})


@router.get("/ap/users/{name}/followers")
async def followers(name: str):
    _on()
    pk = await _member(name)
    url = f"{await _url_of(pk)}/followers"
    return _ap(_collection(url, await state.follower_count(pk)))


@router.get("/ap/users/{name}/following")
async def following(name: str):
    _on()
    pk = await _member(name)
    url = f"{await _url_of(pk)}/following"
    try:
        n = len(await state.following(pk, strict=False))
    except Exception:
        n = 0
    return _ap(_collection(url, n))


_outbox_counts: dict = {}
_count_runs: list = []
COUNTS_PER_MINUTE = 30


@router.get("/ap/users/{name}/outbox")
async def outbox(name: str, page: str = "", max_id: int = 0, after: str = ""):
    """An account's recent public posts, newest first, paged -- what Akkoma and Mastodon read to show
    a profile's posts. It used to answer the count only (always 0), so a profile opened on another
    server showed nothing but what had been delivered to it."""
    _on()
    pk = await _member(name)
    url = f"{await _url_of(pk)}/outbox"
    from app.services.activitypub import outbox as ob
    after = after if re.fullmatch(r"[0-9a-f]{1,64}", after or "") else ""
    if page:
        try:
            items, oldest, last_id = await ob.public_posts(pk, until=max_id, after=after[:64], limit=20)
        except Exception:
            raise HTTPException(503, "could not read the relay")
        here = f"{url}?page=true" + (f"&max_id={max_id}&after={after[:64]}" if max_id else "")
        doc = {"@context": convert.AS_CONTEXT, "id": here,
               "type": "OrderedCollectionPage", "partOf": url, "orderedItems": items}
        if items and oldest:
            doc["next"] = f"{url}?page=true&max_id={oldest}&after={last_id}"
        return _ap(doc)
    # The total is only a label ("123 posts"); counted from one bounded read, kept five minutes.
    hit = _outbox_counts.get(pk)
    now = time.monotonic()
    _count_runs[:] = [t for t in _count_runs if now - t < 60]
    if hit and (now - hit[0] < 300 or len(_count_runs) >= COUNTS_PER_MINUTE):
        total = hit[1]
    elif len(_count_runs) >= COUNTS_PER_MINUTE:
        # The count is a label; anybody can ask for any npub's outbox, and each uncounted one is a
        # 2000-event read. Past the node-wide budget the collection is served without one.
        return _ap({"@context": convert.AS_CONTEXT, "id": url, "type": "OrderedCollection",
                    "first": f"{url}?page=true"})
    else:
        _count_runs.append(now)
        try:
            from app.services import nostr_store, settings_store
            evs = await nostr_store._ws_query(settings_store._port(), [{"kinds": [1, 1068], "authors": [pk], "limit": 2000}],
                                              strict=True)
            total = sum(1 for e in evs if ob.counts_as_public(e))
        except Exception:
            # Unreadable is not "no posts": served without a count, and not remembered.
            return _ap({"@context": convert.AS_CONTEXT, "id": url, "type": "OrderedCollection",
                        "first": f"{url}?page=true"})
        _outbox_counts[pk] = (now, total)
        while len(_outbox_counts) > 5000:
            _outbox_counts.pop(next(iter(_outbox_counts)))
    return _ap({**_collection(url, total), "first": f"{url}?page=true"})


@router.get("/ap/objects/{event_id}")
async def note(event_id: str, request: Request):
    """A member's post, straight from the relay. Anything that is not a member's own public note
    (a mirror, somebody else's event, a DM) is 404 -- this is not a window onto the relay."""
    _on()
    from app.services.activitypub import outbox as ob
    try:
        ev = await ob._event(event_id, strict=True) if re.fullmatch(r"[0-9a-f]{64}", event_id) else None
    except _RELAY_DOWN:
        return _relay_down()                       # never a 404 (cached, and believed) for "could not ask"
    if ev is None and re.fullmatch(r"[0-9a-f]{64}", event_id) and await _deleted_by_member(event_id):
        # Gone, not unknown: 410 + a Tombstone is how a server learns to drop its copy.
        return JSONResponse({"@context": convert.AS_CONTEXT, "id": convert.object_url(config.base_url(), event_id),
                             "type": "Tombstone"}, status_code=410, media_type=config.AP_CONTENT_TYPE)
    if not ev or ev.get("kind") not in ob.POST_KINDS or ob._is_mirror(ev) or ob._protected(ev):
        raise HTTPException(404, "Not Found")
    try:
        shown = bool(actors.handle(ev.get("pubkey", ""))) and await actors.exposed(ev["pubkey"], strict=True)
    except _RELAY_DOWN:
        return _relay_down()
    if not shown:
        raise HTTPException(404, "Not Found")
    if _wants_html(request):
        return RedirectResponse(f"{config.base_url()}/{convert._nevent_or_note(event_id)}", status_code=302)
    me = await _url_of(ev["pubkey"])
    doc, _ctx = await ob.build_object(ev, me)
    if doc is None:
        # A reply deep in a Nostr-only thread: it was never sent, and it is not served either.
        raise HTTPException(404, "Not Found")
    doc["@context"] = convert.AS_CONTEXT
    return _ap(doc)


async def _deleted_by_member(event_id: str) -> bool:
    """Whether a member deleted this event (a kind-5 by an account on the fediverse naming it)."""
    from app.services import nostr_store, settings_store
    try:
        evs = await nostr_store._ws_query(settings_store._port(), [{"kinds": [5], "#e": [event_id], "limit": 5}])
    except Exception:
        return False
    for d in evs or []:
        if d.get("pubkey") and (actors.is_actor(d["pubkey"]) or await actors.exposed(d["pubkey"])):
            return True
    return False


# ------------------------------------------------------------------------------------ inbox

async def _verified(request: Request) -> tuple[dict, str]:
    """(activity, signer actor id), or raise. The signature is checked against the PUBLIC host:
    behind router.lan the upstream request may carry another Host header than the one signed."""
    # Refuse an oversized body BEFORE holding it: request.body() buffers all of it first.
    cl = request.headers.get("content-length") or ""
    if cl.isdigit() and int(cl) > MAX_BODY:
        raise HTTPException(413, "Too large")
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > MAX_BODY:
            raise HTTPException(413, "Too large")
    body = bytes(buf)
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
    # A BLOCKED server is answered BEFORE verification. Its key cannot be fetched (every fetch from a
    # blocked host is refused), so it used to fail as a 401 -- which a server retries for days --
    # and never reached the "accepted and dropped" answer meant for it. Nothing it sent is used.
    key_host = remote.host_of(params.get("keyId") or "")
    if key_host and (config.host_blocked(key_host) or config.host_blocked(remote.host_of(convert.id_of(activity.get("actor"))))):
        raise HTTPException(202, "Accepted")
    # A SUBSCRIBED RELAY PUSHES EVERY POST OF EVERY OTHER MEMBER INSTANCE HERE, and none of it is
    # anything this server stores (nobody here follows those authors -- the inbox would verify each
    # one, fetch keys, and drop it). Verified one by one that is a key fetch and an RSA check per post,
    # and past the per-host budget it is a 429 -- which a relay reads as a dead subscriber and drops.
    # So what a relay RELAYS is acknowledged without work; only its answers to us (Accept/Reject, its
    # own Follow) go on to be verified.
    from app.services.activitypub import relays as _relays
    _inner = activity.get("object") if isinstance(activity.get("object"), dict) else {}
    _answers_us = activity.get("type") in ("Accept", "Reject", "Follow") \
        or (activity.get("type") == "Undo" and _inner.get("type") == "Follow")
    _key_actor = str(params.get("keyId") or "").split("#")[0]
    if _key_actor and not _answers_us and _key_actor in await _relays.relay_actors():
        raise HTTPException(202, "relay")
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
            # Full, the OLDEST are dropped, never everybody's: clearing the table on overflow let
            # anyone re-arm every key's refresh by naming 5000 junk keyIds.
            if len(_refreshed) > 5000:
                for k in sorted(_refreshed, key=_refreshed.get)[:1000]:
                    _refreshed.pop(k, None)
        try:
            owner, pem = await remote.public_key(params["keyId"], refresh=refresh)
            httpsig.verify(request.method, path, headers, body, pem, params)
            return activity, owner
        except (remote.FetchError, httpsig.SignatureError) as e:
            last = e
    # An account that has been DELETED can no longer be fetched, so its own Delete can never
    # verify; answering 401 makes its server retry that forever. It is gone either way. ONLY an
    # account deleting ITSELF: a post's Delete whose key fetch merely failed (a 429, a timeout) must
    # be retried by its sender, or the post it deletes stays here for good.
    actor = convert.id_of(activity.get("actor"))
    if activity.get("type") == "Delete" and isinstance(last, remote.FetchError) \
            and actor and convert.id_of(activity.get("object")) == actor:
        t = asyncio.create_task(inbox.confirm_gone(actor))
        _bg.add(t)
        t.add_done_callback(_bg.discard)
        raise HTTPException(202, "Accepted")
    # The reason is logged, never answered: the error text of a fetch we made on the sender's say-so
    # ("HTTP 404 from …", a connect error) would turn this endpoint into a port scanner.
    logger.info("[activitypub] signature from %s not accepted: %s", remote.host_of(params["keyId"]), last)
    raise HTTPException(401, "Signature not accepted")


_bg: set = set()
_refreshed: dict = {}            # keyId -> when it was last force-refreshed
_hits: dict = {}                 # remote host -> (window start, count)
PER_HOST_PER_MINUTE = 300


def _rate_ok(host: str, *, peek: bool = False) -> bool:
    """A per-server budget: one instance cannot make this node mint puppets and fetch actors
    without limit. Generous -- a busy server sends a burst after every popular post. `peek` asks
    without charging (the connection's budget is only spent by requests that failed to verify).

    Full, the table drops its OLDEST windows, never everybody's: clearing it on overflow let
    anyone reset every budget by naming 10,000 hosts."""
    now = time.monotonic()
    start, n = _hits.get(host, (now, 0))
    if now - start > 60:
        start, n = now, 0
    if peek:
        return n < PER_HOST_PER_MINUTE
    _hits[host] = (start, n + 1)
    if len(_hits) > 10000:
        for k in sorted(_hits, key=lambda k: _hits[k][0])[:2000]:
            _hits.pop(k, None)
    return n < PER_HOST_PER_MINUTE


def _client_ip(request: Request) -> str:
    """Who is actually connected. Behind the reverse proxy the peer is the proxy itself, so the
    address it forwarded is used then -- and only then (a direct client cannot pick its own)."""
    peer = request.client.host if request.client else ""
    # X-Real-IP is set by our proxy. Of X-Forwarded-For only the LAST entry is: the proxy appends the
    # address it saw, while everything before it is whatever the client chose to send.
    fwd = (request.headers.get("x-real-ip") or "").strip() or \
        (request.headers.get("x-forwarded-for") or "").split(",")[-1].strip()
    try:
        import ipaddress
        addr = ipaddress.ip_address(peer)
        if fwd and (addr.is_private or addr.is_loopback):
            return fwd
    except ValueError:
        pass
    return peer


async def _receive(request: Request) -> Response:
    _on()
    # The budget is charged to WHO SIGNED, after verification. Charged to the keyId's host before it,
    # anybody could spend a real instance's budget by naming it in junk requests and make every
    # genuine delivery from it 429. Unverified traffic is charged to the connection instead.
    ip = "ip:" + _client_ip(request)
    if not _rate_ok(ip, peek=True):
        return Response(status_code=429, headers={"Retry-After": "60"})
    try:
        activity, signer = await _verified(request)
    except _RELAY_DOWN:
        return _relay_down()
    except HTTPException as e:
        if e.status_code == 202:
            if e.detail != "relay":            # a subscribed relay's traffic is expected, and costs nothing
                _rate_ok(ip)                   # unverified, and it may cost one fetch (confirm_gone)
            return Response(status_code=202)
        if e.status_code in (400, 401, 413):
            _rate_ok(ip)
        raise
    host = remote.host_of(signer)
    if not _rate_ok(host):
        return Response(status_code=429, headers={"Retry-After": "60"})
    if config.is_own_host(host) or config.host_blocked(host):
        return Response(status_code=202)          # blocked instances: accepted and dropped, so they stop
    if convert.id_of(activity.get("actor")) != signer:
        # A FORWARDED reply (Mastodon relays its users' replies, signed by itself). Refusing it made
        # the forwarder retry for days; it is taken as a pointer and fetched from its own server.
        if activity.get("type") in ("Create", "Update") and convert.id_of(activity.get("object")):
            if not inbox.schedule({"type": inbox.FORWARDED, "object": convert.id_of(activity.get("object"))}, signer):
                return Response(status_code=503, headers={"Retry-After": "120"})
            return Response(status_code=202)
        # A forwarded DELETE (Mastodon forwards a reply's deletion too): confirmed with the post's own
        # server. A 401 here was retried for days, exactly like the forwarded Create used to be.
        if activity.get("type") == "Delete" and convert.id_of(activity.get("object")):
            if not inbox.schedule({"type": inbox.FORWARDED_DELETE, "object": convert.id_of(activity.get("object"))}, signer):
                return Response(status_code=503, headers={"Retry-After": "120"})
            return Response(status_code=202)
        raise HTTPException(401, "Signed by somebody other than the actor")
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
    # A blocked account (a `user@host` line, or its puppet blocked on the relay) is not looked up:
    # this would mint it a puppet and hand it to the member to follow.
    if await inbox.blocked_actor(convert.id_of(doc)):
        raise HTTPException(404, "That account is blocked on this server")
    p = await inbox._puppet(doc)
    if not p:
        raise HTTPException(404, "That account could not be mirrored")
    return {"pubkey": p["pubkey_hex"], "npub": p["npub"], "acct": p.get("acct", ""), "actor": convert.id_of(doc)}


@router.post("/api/activitypub/import-following")
async def import_following(request: Request, user=Depends(get_current_user)):
    """A fediverse account's public following list, as puppet pubkeys for the client to add to its
    contact list (see importer.py). Body: {"account": "name@server"}."""
    _on()
    try:
        body = await request.json()
    except Exception:
        body = {}
    handle = str((body or {}).get("account") or "").strip() if isinstance(body, dict) else ""
    # What arrived, never who: the host only (a public server name), so a report of "it said X"
    # can be matched to what the browser actually sent.
    logger.info("[activitypub] import-following: %s",
                f"host={handle.rpartition('@')[2][:80]}" if handle else "no address")
    if not handle:
        raise HTTPException(400, "Type the account to import from, as name@server")
    # One import per member at a time, and not more than one a minute: each resolves up to 5000
    # actors from their own servers, which is this node's bandwidth spent on the member's behalf.
    now = time.monotonic()
    if user.id in _importing or now - _imported.get(user.id, -1e9) < 60:
        raise HTTPException(429, "An import is already running or just ran -- wait a minute and try again")
    _importing.add(user.id)
    try:
        return await _import(handle)
    finally:
        _importing.discard(user.id)
        _imported[user.id] = time.monotonic()


_importing: set = set()
_imported: dict = {}


async def _import(handle: str) -> dict:
    from app.database import SessionLocal
    from app.services.activitypub import importer
    try:
        accounts, instance_url = await importer.public_following(handle)
    except ValueError as e:
        logger.info("[activitypub] import-following refused: %s", str(e)[:200])
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"Could not read who that account follows: {type(e).__name__}")
    db = SessionLocal()
    try:
        people = await importer.puppets_for(db, accounts, instance_url)
    finally:
        db.close()
    return {"following": len(accounts), "people": people}


# THE RETIRED PLEROMA'S FAVICON ADDRESS. poster.place ran Pleroma before this server, and every
# fediverse server that met it then STORED that instance's favicon URL (Akkoma: the account's
# `akkoma.instance.favicon`) and refreshes it only when it decides to. The file behind it is gone,
# so posts from here showed no instance icon at all on those servers. Answering that one exact
# address with our icon fixes every stale record at once; nothing else under /media/ is served.
_OLD_PLEROMA_FAVICON = "/media/cb/93/65/cb9365f4ea06831500dde507896aa018db5da207979abdc44add6cc00a67e2e9.webp"
_ICON = __import__("pathlib").Path(__file__).resolve().parents[2] / "static" / "icon-192.png"


@router.get(_OLD_PLEROMA_FAVICON, include_in_schema=False)
async def old_pleroma_favicon():
    return FileResponse(str(_ICON), media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


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
                         "followers": await state.follower_count(pk) if member else 0})
    try:
        report = await nostr_store.get_doc(settings_store._port(), "pcai:ap:stats",
                                           seckey=settings_store._operator_seckey(None)) or {}
    except Exception:
        report = {}
    from app.services.activitypub import relays
    try:
        subs = await relays.subscriptions(strict=False)
    except Exception:
        subs = {}
    relay_rows = [{"inbox": i, "state": (subs.get(i) or {}).get("state") or "not yet asked"} for i in relays.configured()]
    return {"enabled": config.enabled(), "domain": config.domain(), "base": base,
            "relays": relay_rows, "relay_scope": relays.scope(),
            "webfinger": f"{base}/.well-known/webfinger?resource=acct:NAME@{config.domain()}" if base else "",
            "blocked_instances": sorted(config.blocked_domains()), "members": rows, "delivery": report}
