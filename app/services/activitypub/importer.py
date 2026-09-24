"""Import who a member follows on their linked Pleroma/Mastodon account -- or on any account whose
follow list is public, by its address (`public_following`).

The server's half only: it reads the account's following list (with the member's own linked token,
the same credential the bridge's write-back uses) and gives each account its puppet identity -- the
bridge's own `ensure_puppet`, so an account already mirrored keeps the key it has. It returns the
puppets' pubkeys; the CLIENT adds them to the member's contact list, because a kind-3 is signed by
the member and this server does not hold their key. The delivery loop then turns the new contact
list into ActivityPub Follows.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

from app.services import pleroma_service, settings_store
from app.services.activitypub import config

MAX_ACCOUNTS = 5000
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


async def following(instance_url: str, token: str) -> list[dict]:
    """Every account the linked account follows (Mastodon-API account objects), all pages."""
    me = await pleroma_service.verify_credentials(instance_url, token)
    if not isinstance(me, dict) or not me.get("id"):
        raise ValueError("the linked account could not be read -- reconnect it in Settings")
    return await _pages(instance_url.rstrip("/"), str(me["id"]), token)


_HOST = re.compile(r"[a-z0-9.-]+\.[a-z0-9-]+(:\d{1,5})?", re.I)


async def public_following(handle: str) -> tuple[list[dict], str]:
    """(accounts, instance url) for ANY account's public follow list, by its address -- no login.

    For somebody who has already disconnected (or never linked) their old account: a follow list
    is public on Pleroma, Akkoma, Mastodon and GoToSocial unless its owner hid it, through the same
    Mastodon API the linked import reads. The host goes through the same guard as every other
    fediverse fetch (https, public address, not blocked, not this node)."""
    from app.services.activitypub import remote
    user, _, host = (handle or "").strip().lstrip("@").partition("@")
    host = host.strip().lower()
    if not user or not _HOST.fullmatch(host or ""):
        raise ValueError("Type the account as name@server")
    base = f"https://{host}"
    try:
        await remote._check(base + "/")
    except remote.FetchError as e:
        raise ValueError(f"Cannot read {host}: {e}") from e
    async with httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False) as client:
        r = await client.get(f"{base}/api/v1/accounts/lookup", params={"acct": user})
    if r.status_code == 404:
        raise ValueError(f"{user}@{host} was not found")
    if r.status_code != 200:
        raise ValueError(f"{host} did not answer the lookup (HTTP {r.status_code}) -- "
                         "it may not be a Mastodon-compatible server")
    me = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not isinstance(me, dict) or not me.get("id"):
        raise ValueError(f"{host} did not return an account for {user}")
    out = await _pages(base, str(me["id"]), None)
    if not out and (me.get("following_count") or 0) > 0:
        raise ValueError(f"{user}@{host} keeps its follow list private -- link the account instead")
    return out, base


async def _pages(base: str, account_id: str, token: str | None) -> list[dict]:
    url = f"{base}/api/v1/accounts/{account_id}/following?limit=80"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    out: list = []
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        while url and len(out) < MAX_ACCOUNTS:
            r = await client.get(url, headers=headers)
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
    """[{pubkey, acct}] for each account -- skipping our own users and blocked instances."""
    from app.services.fedi_bridge_identity import acct_of, ensure_puppet
    host = (urlparse(instance_url).hostname or "").lower()
    port = settings_store._port()
    out, seen = [], set()
    for a in accounts:
        acct = acct_of(a, host)
        acct_host = acct.partition("@")[2].lower()
        if not acct or config.is_own_host(acct_host) or config.host_blocked(acct_host):
            continue
        p = await ensure_puppet(db, port, a, host)
        if p and p["pubkey_hex"] not in seen:
            seen.add(p["pubkey_hex"])
            out.append({"pubkey": p["pubkey_hex"], "acct": p.get("acct") or acct})
    return out
