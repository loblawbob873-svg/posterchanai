"""Import who a member follows on their linked Pleroma/Mastodon account.

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
    base = instance_url.rstrip("/")
    url = f"{base}/api/v1/accounts/{me['id']}/following?limit=80"
    out: list = []
    async with httpx.AsyncClient(timeout=25, follow_redirects=False) as client:
        while url and len(out) < MAX_ACCOUNTS:
            r = await client.get(url, headers={"Authorization": f"Bearer {token}"})
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
