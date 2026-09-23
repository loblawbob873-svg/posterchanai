"""Talking to other servers: fetch an actor or object, resolve a handle, deliver an activity.

Every fetch is SSRF-guarded on EVERY redirect hop (the address is attacker-supplied: it comes out
of an activity somebody sent us), is size-capped, and is refused for an instance the relay blocks.
GETs are signed with the instance actor's key, because servers running "authorized fetch" answer an
unsigned GET with 401.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict
from urllib.parse import urljoin, urlparse

import httpx

from app.services.activitypub import config, httpsig, state

logger = logging.getLogger(__name__)

MAX_BYTES = 1024 * 1024
_TIMEOUT = httpx.Timeout(12.0, connect=6.0)
_ACCEPT = f'{config.AP_CONTENT_TYPE}, {config.LD_CONTENT_TYPE};q=0.9'
_USER_AGENT = "PosterChan-ActivityPub (+https://github.com/loblawbob873-svg/posterchanai)"
_ACTOR_TTL = 3600.0
_actors: "OrderedDict[str, tuple[float, dict]]" = OrderedDict()


class FetchError(Exception):
    pass


def host_of(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


async def _check(url: str) -> None:
    from app.services.rss_service import is_safe_host, looks_fetchable
    p = urlparse(url or "")
    if p.scheme != "https" or not looks_fetchable(url):
        raise FetchError(f"not a fetchable https address: {url[:120]}")
    if config.host_blocked(p.hostname or "") or config.is_own_host(p.hostname or ""):
        raise FetchError(f"{p.hostname} is blocked or is this node")
    if not await asyncio.to_thread(is_safe_host, url):
        raise FetchError(f"{p.hostname} resolves to a private address")


async def instance_key() -> tuple[str, str]:
    doc = await state.keypair("instance")
    return f"{config.base_url()}/ap/actor#main-key", doc["priv"]


async def fetch_json(url: str, *, signed: bool = True) -> dict:
    """GET an ActivityPub document. Raises FetchError for anything but a JSON object."""
    key_id, priv = (await instance_key()) if signed else ("", "")
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False, trust_env=False) as client:
        for _hop in range(4):
            await _check(url)
            headers = {"Accept": _ACCEPT, "User-Agent": _USER_AGENT}
            if signed:
                headers.update(httpsig.sign("GET", url, key_id=key_id, private_pem=priv))
            try:
                async with client.stream("GET", url, headers=headers) as r:
                    if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                        nxt = urljoin(url, r.headers["location"])
                        # NEVER across hosts. A document's authority is the host that served it, so an
                        # open redirect on one server must not let another server answer in its name.
                        if host_of(nxt) != host_of(url):
                            raise FetchError("redirect to another host")
                        url = nxt
                        continue
                    if r.status_code != 200:
                        raise FetchError(f"HTTP {r.status_code} from {host_of(url)}")
                    body = bytearray()
                    async for chunk in r.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            raise FetchError("document too large")
            except httpx.HTTPError as e:
                raise FetchError(f"{type(e).__name__} fetching {host_of(url)}") from e
            try:
                doc = json.loads(bytes(body))
            except ValueError as e:
                raise FetchError("not JSON") from e
            if not isinstance(doc, dict):
                raise FetchError("not a JSON object")
            return doc
    raise FetchError("too many redirects")


async def actor(uri: str, *, refresh: bool = False) -> dict:
    """A remote actor document, cached for an hour. Its `id` must be the address it was fetched as
    (after redirects the id is re-checked by the caller where identity matters)."""
    uri = (uri or "").split("#")[0]
    hit = _actors.get(uri)
    if hit and not refresh and time.monotonic() - hit[0] < _ACTOR_TTL:
        _actors.move_to_end(uri)
        return hit[1]
    doc = await fetch_json(uri)
    if doc.get("type") not in ("Person", "Service", "Application", "Group", "Organization"):
        raise FetchError(f"{uri} is not an actor")
    # The document must BE the actor that was asked for, not merely live on the same host: an actor
    # document can claim any id, and that id is what every later check trusts.
    if str(doc.get("id") or "").split("#")[0] != uri:
        raise FetchError("the actor document is not the actor that was asked for")
    _actors[uri] = (time.monotonic(), doc)
    _actors.move_to_end(uri)
    while len(_actors) > 4000:
        _actors.popitem(last=False)
    return doc


async def public_key(key_id: str, *, refresh: bool = False) -> tuple[str, str]:
    """(owner actor id, public key PEM) for a signature's keyId.

    THE WHOLE INBOX'S TRUST RESTS HERE, so every link is checked: the key and its owner are on the
    SAME host as the keyId (a server speaks only for its own accounts); a document fetched from an
    address must carry that address as its id (fetch_json refuses cross-host redirects, `actor`
    refuses a mismatched id); and the owner must publish exactly this key. A key that merely names
    an owner proves nothing about the owner -- without these, anyone could host a document claiming
    to be somebody else's actor and sign as them."""
    key_host = host_of(key_id)
    base = key_id.split("#")[0]
    if "#" in key_id:
        owner = await actor(base, refresh=refresh)
    else:
        doc = await fetch_json(base)
        if str(doc.get("id") or "") != key_id:
            raise FetchError("the key document is not the key that was named")
        if doc.get("type") in ("Key", "CryptographicKey") or ("owner" in doc and "publicKeyPem" in doc):
            owner = await actor(str(doc.get("owner") or ""), refresh=refresh)
        elif doc.get("type") in ("Person", "Service", "Application", "Group", "Organization"):
            owner = await actor(base, refresh=refresh)
        else:
            raise FetchError("the keyId is neither a key nor an actor")
    if host_of(owner.get("id")) != key_host:
        raise FetchError("the key and its owner are on different hosts")
    pk = owner.get("publicKey")
    keys = pk if isinstance(pk, list) else [pk]
    for k in keys:
        if isinstance(k, dict) and k.get("id") == key_id and k.get("publicKeyPem"):
            if (k.get("owner") or owner.get("id")) != owner.get("id"):
                raise FetchError("key owner mismatch")
            return str(owner.get("id")), str(k["publicKeyPem"])
    raise FetchError("the owner does not publish that key")


async def fetch_object(uri: str) -> dict:
    """An object from its OWN server, and only if it is the object that was asked for -- what makes
    an embedded or boosted note trustworthy when the sender is not its author."""
    doc = await fetch_json(uri)
    if str(doc.get("id") or "") != uri:
        raise FetchError("the fetched object is not the object that was asked for")
    return doc


def inbox_of(doc: dict) -> str:
    ep = doc.get("endpoints") if isinstance(doc.get("endpoints"), dict) else {}
    return str(ep.get("sharedInbox") or doc.get("inbox") or "")


async def webfinger(handle: str) -> str:
    """`user@host` → that account's actor URI, via the host's WebFinger."""
    handle = (handle or "").strip().lstrip("@")
    user, _, host = handle.partition("@")
    if not user or not host or "/" in host:
        raise FetchError("expected user@host")
    url = f"https://{host}/.well-known/webfinger?resource=acct:{user}@{host}"
    await _check(url)
    body = bytearray()
    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False, trust_env=False) as client:
        try:
            async with client.stream("GET", url, headers={"Accept": "application/jrd+json, application/json",
                                                          "User-Agent": _USER_AGENT}) as r:
                if r.status_code != 200:
                    raise FetchError(f"{host} does not know {user}")
                async for chunk in r.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_BYTES:
                        raise FetchError("WebFinger answer too large")
        except httpx.HTTPError as e:
            raise FetchError(f"{type(e).__name__} asking {host}") from e
    try:
        doc = json.loads(bytes(body))
    except ValueError as e:
        raise FetchError("unreadable WebFinger answer") from e
    links = doc.get("links") if isinstance(doc, dict) else None
    for link in links if isinstance(links, list) else []:
        if not isinstance(link, dict):
            continue
        kind = str(link.get("type") or "")
        if link.get("rel") == "self" and ("activity+json" in kind or "activitystreams" in kind):
            return str(link.get("href") or "")
    raise FetchError(f"{handle} has no ActivityPub actor")


async def deliver(inbox: str, activity: dict, *, key_id: str, private_pem: str) -> int:
    """POST a signed activity. Returns the HTTP status (0 when it never got an answer)."""
    try:
        await _check(inbox)
    except FetchError as e:
        logger.info("[activitypub] not delivering to %s: %s", host_of(inbox), e)
        return 0
    body = json.dumps(activity, separators=(",", ":")).encode()
    headers = {"Content-Type": config.AP_CONTENT_TYPE, "Accept": _ACCEPT, "User-Agent": _USER_AGENT}
    headers.update(httpsig.sign("POST", inbox, key_id=key_id, private_pem=private_pem, body=body))
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False, trust_env=False) as client:
            r = await client.post(inbox, content=body, headers=headers)
            return r.status_code
    except httpx.HTTPError as e:
        logger.info("[activitypub] delivery to %s failed: %s", host_of(inbox), type(e).__name__)
        return 0
