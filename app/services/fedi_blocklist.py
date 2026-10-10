"""ONE reading of the fediverse blocklists, for the Pleroma bridge and the ActivityPub server alike.

An admin's list is typed by hand and grows for years, so it is read the way people write it:

  * `bad.example`, `@bad.example`, `https://bad.example/`, `*.bad.example`  -> that instance and
    every subdomain of it;
  * `someone@bad.example`, `https://bad.example/@someone`                   -> THAT ACCOUNT only
    (it used to be read as an instance -- the bridge compared it to hosts and never matched, so the
    account was never blocked; a URL parser would have blocked the whole instance instead);
  * internationalised names (`嘟文.com`)                                     -> both spellings, because
    servers send hosts and handles in punycode (`xn--…`), which never matched the Unicode entry.

Separated by newlines, commas or spaces; `#` starts a comment.
"""
from __future__ import annotations

from functools import lru_cache


def _host(h: str) -> str:
    h = (h or "").strip().lower().rstrip(".")
    if h.startswith("[") and "]" in h:
        return h[1:h.index("]")]
    if h.count(":") == 1:
        h = h.split(":", 1)[0]
    return h


def _spellings(host: str) -> set:
    """A host in every form it may arrive in: as written, punycode, and Unicode."""
    out = {host}
    try:
        out.add(host.encode("idna").decode("ascii"))
    except (UnicodeError, ValueError):
        pass
    try:
        if "xn--" in host:
            out.add(host.encode("ascii").decode("idna"))
    except (UnicodeError, ValueError):
        pass
    return {s for s in out if s}


def normalize(tok: str) -> str:
    """ONE written entry as what it blocks: `host` for an instance, `user@host` for one account, "" for
    nothing. The blocker (parse) and Admin's list (relay_lists, kind "fedi") both read entries through
    this, so a row on screen is exactly what the blocker enforces."""
    # Stray punctuation from a pasted list (`bad.example;`, a quoted host) kept verbatim matched
    # nothing, silently.
    t = (tok or "").strip().strip("\"'`;<>()[]{}|").lower()
    for pre in ("https://", "http://", "wss://", "ws://"):
        if t.startswith(pre):
            t = t[len(pre):]
    host_part, _, path = t.partition("/")
    if path.startswith("@") and len(path) > 1:
        # A pasted PROFILE link (https://host/@bob) means that account, not its instance.
        t = f"{path[1:].split('/')[0].split('@')[0]}@{host_part}"
    else:
        t = host_part
    # `*.bad.example` and `*bad.example` both mean the instance and its subdomains.
    t = t.lstrip("@").removeprefix("*.").lstrip("*").strip(".")
    if not t:
        return ""
    if "@" in t:                                   # user@host -- one account
        user, _, host = t.partition("@")
        host = _host(host)
        return f"{user}@{host}" if user and host else ""
    return _host(t)


def tokens(raw: str) -> list:
    """The entries as written: newlines, commas or spaces separate them, `#` starts a comment."""
    out = []
    for line in (raw or "").splitlines():
        out.extend(line.split("#", 1)[0].replace(",", " ").split())
    return out


@lru_cache(maxsize=16)
def parse(raw: str) -> tuple[frozenset, frozenset]:
    """(blocked instance hosts, blocked accounts as user@host), every spelling of each."""
    hosts, accounts = set(), set()
    for tok in tokens(raw):
        t = normalize(tok)
        if "@" in t:
            user, _, host = t.partition("@")
            accounts |= {f"{user}@{h}" for h in _spellings(host)}
        elif t:
            hosts |= _spellings(t)
    return frozenset(hosts), frozenset(accounts)


def host_blocked(host: str, hosts) -> bool:
    """`host` is a blocked instance or a subdomain of one (a.b.c is covered by b.c)."""
    for h in _spellings(_host(host)):
        if any(h == d or h.endswith("." + d) for d in hosts):
            return True
    return False


def account_blocked(acct: str, accounts, hosts=frozenset()) -> bool:
    """`user@host` is a blocked account, or lives on a blocked instance."""
    a = (acct or "").strip().lower().lstrip("@")
    # The HOST is after the LAST `@`: a username is the sender's text, and split at the first `@`,
    # `bob@x@evil.com` compared host `x@evil.com` and slipped past a `bob@evil.com` line.
    user, _, host = a.rpartition("@")
    if not user or not host:
        return False
    if host_blocked(host, hosts):
        return True
    return any(f"{user}@{h}" in accounts for h in _spellings(_host(host)))


def _blocked_among(rows, raw: str) -> list:
    hosts, accounts = parse(raw or "")
    if not hosts and not accounts:
        return []
    return [r.pubkey_hex for r in rows
            if r.pubkey_hex and r.acct and account_blocked(r.acct, accounts, hosts)]


def blocked_puppet_pubkeys(raw: str) -> list:
    """The puppet keys of every fediverse account this list blocks -- an instance line covers all its
    accounts, a `user@host` line one. Their posts are what an instance block has to take back out of the
    relay: blocking only stops NEW deliveries, and the stored ones stayed in timelines ("i blocked
    baraag.net but posts are still in posterchan" -- 115 accounts, 100 stored posts). Raises
    relay_reader.Unavailable when the puppet registry cannot be read (the caller purges nothing)."""
    hosts, accounts = parse(raw or "")
    if not hosts and not accounts:
        return []
    from app.services import fedi_tables
    return _blocked_among(fedi_tables.all_puppets(), raw)


async def ablocked_puppet_pubkeys(raw: str) -> list:
    hosts, accounts = parse(raw or "")
    if not hosts and not accounts:
        return []
    from app.services import fedi_tables
    return _blocked_among(await fedi_tables.aall_puppets(), raw)
