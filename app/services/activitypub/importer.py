"""Import who an old fediverse account follows, by its address (`public_following`): any account
whose follow list is public, on Pleroma, Akkoma, Mastodon or GoToSocial -- no login.

The server's half only: it reads the account's following list and gives each account its puppet
identity (`ensure_puppet`, so an account that already has one keeps its key). It returns the
puppets' pubkeys; the CLIENT adds them to the member's contact list, because a kind-3 is signed by
the member and this server does not hold their key. The delivery loop then turns the new contact
list into ActivityPub Follows.
"""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urlparse

from app.services import settings_store
from app.services.activitypub import config

MAX_ACCOUNTS = 5000
MAX_PAGE_BYTES = 4 * 1024 * 1024
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


_HOST = re.compile(r"[a-z0-9.-]+\.[a-z0-9-]+(:\d{1,5})?", re.I)
_PAGE_SECONDS = 30


class _Answer:
    def __init__(self, status: int, headers, body: bytes):
        self.status_code, self.headers, self.content = status, headers, body

    def json(self):
        return json.loads(self.content)


async def _get(client, url: str, **kw) -> _Answer:
    """GET with the size cap enforced WHILE reading and a total time limit. `client.get()` buffers
    the whole body first, so a server a member typed could stream gigabytes into this process before
    the size check ever ran (or drip a byte every few seconds and hold the import open for ever)."""
    from app.services.activitypub import remote
    try:
        async with asyncio.timeout(_PAGE_SECONDS):
            async with client.stream("GET", url, **kw) as r:
                body = bytearray()
                async for chunk in r.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_PAGE_BYTES:
                        raise ValueError(f"{urlparse(url).hostname} answered with something too large to read")
                return _Answer(r.status_code, r.headers, bytes(body))
    except TimeoutError:
        raise ValueError(f"{urlparse(url).hostname} took too long to answer")
    except remote.httpx.HTTPError as e:
        raise ValueError(f"Could not reach {urlparse(url).hostname}: {type(e).__name__}")


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
        r = await _get(client, f"{base}/api/v1/accounts/lookup", params={"acct": user})
    if r.status_code == 404:
        raise ValueError(f"{user}@{host} was not found")
    if r.status_code != 200:
        raise ValueError(f"{host} did not answer the lookup (HTTP {r.status_code}) -- "
                         "it may not be a Mastodon-compatible server")
    try:
        me = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    except ValueError:
        me = {}
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
            r = await _get(client, url, headers=headers)
            if not token and r.status_code in (401, 403):
                break                                  # a hidden list: the caller says so
            if r.status_code != 200:
                if out:
                    break                              # keep what was read; a later page failing loses nothing
                raise ValueError(f"{urlparse(url).hostname} answered HTTP {r.status_code}")
            try:
                page = r.json()
            except ValueError:
                break
            if not isinstance(page, list) or not page:
                break
            out += [a for a in page if isinstance(a, dict)]
            m = _NEXT.search(r.headers.get("link", ""))
            nxt = m.group(1) if m else ""
            # Pages stay on the account's own instance -- the Link header is the instance's to set,
            # but never ours to follow off it with the member's token attached.
            # And https only: a plain-http "next" failed the fetch guard and threw away every page read.
            url = nxt if nxt.startswith("https://") and urlparse(nxt).hostname == urlparse(base).hostname else ""
    return out[:MAX_ACCOUNTS]


IMPORT_BUDGET_S = 80    # under Cloudflare's 100s request limit, with room for the relay write after


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
    home = urlparse(instance_url or "").hostname or ""

    def usable(url):
        host = urlparse(url).hostname or ""
        return url.startswith("https://") and host and not config.is_own_host(host) and not config.host_blocked(host)

    def addresses(a):
        """Where this account's actor may be asked for, best first. Mastodon sends `uri` (the actor
        id). PLEROMA AND AKKOMA SEND NO `uri` AT ALL -- their actor id is in `url` -- and reading `uri`
        alone dropped every account from such a server before a single request was made ("0 of 161
        there", importing from shitpost.cloud). On Mastodon `url` is the HTML profile, which
        remote.actor rightly refuses (the document is not the actor asked for), so the last resort is
        the account's own WebFinger. Every address is still only an address: the identity always
        comes from the document its own server returns."""
        if not isinstance(a, dict):
            return []
        out = []
        for key in ("uri", "url"):
            v = str(a.get(key) or "").strip()
            if usable(v) and v not in out:
                out.append(v)
        acct = str(a.get("fqn") or a.get("acct") or "").strip().lstrip("@")
        if acct and "@" not in acct and home:
            acct = f"{acct}@{home}"                     # a local account's acct carries no host
        host = acct.partition("@")[2]
        if acct.count("@") == 1 and host and not config.is_own_host(host) and not config.host_blocked(host):
            out.append("acct:" + acct)
        return out
    wanted, keys = [], set()
    for a in accounts:
        adr = addresses(a)
        if adr and adr[0] not in keys:
            keys.add(adr[0])
            wanted.append(adr)
    slots = asyncio.Semaphore(16)

    async def resolve(adr):
        async with slots:
            for where in adr:
                try:
                    uri = await asyncio.wait_for(remote.webfinger(where[5:]), timeout=15) \
                        if where.startswith("acct:") else where
                    if not usable(uri):
                        continue
                    return await asyncio.wait_for(remote.actor(uri), timeout=20)
                except Exception:
                    continue
            return None
    # ONE BUDGET FOR THE WHOLE LIST. poster.place sits behind Cloudflare, which ends any request at
    # 100s (a 524 with nothing imported), and resolving a real 70-account list took 66s at 8 at a
    # time. Whatever resolved inside the budget is imported; the rest is simply not in this answer
    # (the page says "N of M there"), and running the import again picks them up.
    tasks = [asyncio.ensure_future(resolve(a)) for a in wanted]
    if tasks:
        await asyncio.wait(tasks, timeout=IMPORT_BUDGET_S)
    docs = []
    for t in tasks:
        if t.done() and not t.cancelled() and t.exception() is None:
            docs.append(t.result())
        else:
            t.cancel()
    out, seen = [], set()
    for doc in docs:
        if not doc:
            continue
        account = convert.account_from_actor(doc)
        acct = account.get("acct") or ""
        if not acct or config.account_blocked(acct):
            continue
        from app.services.activitypub import actors
        if actors.puppet_blocked(account["uri"], acct):
            continue                                   # blocked from the client: never re-followed
        p = await ensure_puppet(db, port, account, remote.host_of(account["uri"]))
        if p and p["pubkey_hex"] not in seen:
            seen.add(p["pubkey_hex"])
            out.append({"pubkey": p["pubkey_hex"], "acct": p.get("acct") or acct})
    return out
