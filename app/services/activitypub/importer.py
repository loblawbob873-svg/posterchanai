"""Import who an old fediverse account follows, by its address (`public_following`): any account
whose follow list is public, on Pleroma, Akkoma, Mastodon or GoToSocial -- no login.

The server's half only: it reads the account's following list and gives each account its puppet
identity (`ensure_puppet`, so an account that already has one keeps its key). It returns the
puppets' pubkeys; the CLIENT adds them to the member's contact list, because a kind-3 is signed by
the member and this server does not hold their key. The delivery loop then turns the new contact
list into ActivityPub Follows.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from app.services import settings_store
from app.services.activitypub import config

MAX_ACCOUNTS = 5000
MAX_PAGE_BYTES = 4 * 1024 * 1024
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


_HOST = re.compile(r"[a-z0-9.-]+\.[a-z0-9-]+(:\d{1,5})?", re.I)


async def public_following(handle: str) -> tuple[list[dict], str]:
    """(accounts, instance url) for ANY account's public follow list, by its address -- no login.

    A follow list is public on Pleroma, Akkoma, Mastodon and GoToSocial unless its owner hid it. The host goes through the same guard as every other
    fediverse fetch (https, public address, not blocked, not this node)."""
    from app.services.activitypub import remote
    user, _, host = (handle or "").strip().lstrip("@").partition("@")
    host = host.strip().lower()
    if not user or not _HOST.fullmatch(host or ""):
        raise ValueError("Type the account as name@server")
    base = f"https://{host}"
    if config.is_own_host(host):
        raise ValueError(f"{user}@{host} is this server -- type your OLD account, e.g. name@your.old.server")
    try:
        await remote._check(base + "/")
    except remote.FetchError as e:
        raise ValueError(f"Cannot read {host}: {e}") from e
    async with remote.client(timeout=25) as client:
        r = await client.get(f"{base}/api/v1/accounts/lookup", params={"acct": user})
    if r.status_code == 404:
        raise ValueError(f"{user}@{host} was not found")
    if r.status_code != 200:
        raise ValueError(f"{host} did not answer the lookup (HTTP {r.status_code}) -- "
                         "it may not be a Mastodon-compatible server")
    if len(r.content) > MAX_PAGE_BYTES:
        raise ValueError(f"{host} answered the lookup with something too large to be an account")
    me = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not isinstance(me, dict) or not me.get("id"):
        raise ValueError(f"{host} did not return an account for {user}")
    out = await _pages(base, str(me["id"]), None)
    if not out and (me.get("following_count") or 0) > 0:
        raise ValueError(f"{user}@{host} keeps its follow list private -- make it public there for a moment, then import")
    return out, base


async def _pages(base: str, account_id: str, token: str | None) -> list[dict]:
    url = f"{base}/api/v1/accounts/{account_id}/following?limit=80"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    out: list = []
    from app.services.activitypub import remote
    async with remote.client(timeout=25) as client:
        while url and len(out) < MAX_ACCOUNTS:
            try:
                await remote._check(url)          # every page, not only the first address
            except remote.FetchError as e:
                raise ValueError(f"Cannot read {urlparse(url).hostname}: {e}") from e
            r = await client.get(url, headers=headers)
            if len(r.content) > MAX_PAGE_BYTES:
                raise ValueError("that server answered with a page too large to read")
            if not token and r.status_code in (401, 403):
                break                                  # a hidden list: the caller says so
            r.raise_for_status()
            page = r.json()
            if not isinstance(page, list) or not page:
                break
            out += [a for a in page if isinstance(a, dict)]
            m = _NEXT.search(r.headers.get("link", ""))
            nxt = m.group(1) if m else ""
            # Pages stay on the account's own instance -- the Link header is the instance's to set,
            # but never ours to follow off it with the member's token attached.
            url = nxt if nxt and urlparse(nxt).hostname == urlparse(base).hostname else ""
    return out[:MAX_ACCOUNTS]


async def puppets_for(db, accounts: list, instance_url: str) -> list[dict]:
    """[{pubkey, acct}] for each account -- skipping our own users and blocked instances.

    NOTHING in the list is trusted: it is a Mastodon-API answer from a server the member typed, so
    every field in it -- `uri`, `acct`, the name, the avatar -- is that server's to invent. Taken as
    sent, one answer could claim `uri: https://mastodon.social/users/alice` and republish Alice's
    Nostr profile with its own name, picture and bio (and her NIP-05 name). So each account is only
    an ADDRESS here: its actor is fetched from its own server (`remote.actor` demands the document
    carry the id it was fetched as) and the identity comes from that document, exactly as for an
    account that sends us an activity."""
    import asyncio
    from app.services.activitypub import convert, remote
    from app.services.fedi_bridge_identity import ensure_puppet
    port = settings_store._port()
    uris = []
    for a in accounts:
        uri = str(a.get("uri") or "").strip() if isinstance(a, dict) else ""
        host = urlparse(uri).hostname or ""
        if uri.startswith("https://") and host and not config.is_own_host(host) \
                and not config.host_blocked(host) and uri not in uris:
            uris.append(uri)
    slots = asyncio.Semaphore(8)

    async def resolve(uri):
        async with slots:
            try:
                return await asyncio.wait_for(remote.actor(uri), timeout=20)
            except Exception:
                return None
    docs = await asyncio.gather(*(resolve(u) for u in uris))
    out, seen = [], set()
    for doc in docs:
        if not doc:
            continue
        account = convert.account_from_actor(doc)
        acct = account.get("acct") or ""
        if not acct or config.account_blocked(acct):
            continue
        p = await ensure_puppet(db, port, account, remote.host_of(account["uri"]))
        if p and p["pubkey_hex"] not in seen:
            seen.add(p["pubkey_hex"])
            out.append({"pubkey": p["pubkey_hex"], "acct": p.get("acct") or acct})
    return out
