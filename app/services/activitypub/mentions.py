"""Turn fediverse @mentions into Nostr profile links.

A fediverse note carries its mentions as plain text plus a Mention tag list. Stored as-is the names
are dead text, so each mentioned account is given its puppet identity (the same deterministic key
every path uses), its `@handle` in the text becomes a `nostr:npub…` reference -- which every client
renders as a profile link -- and it is p-tagged.

Moved here from the retired Pleroma bridge, which had learned these rules the hard way; the handle
rules are kept verbatim, the Pleroma-only parts (the read account's mute list, linked-account lookups)
are gone with the bridge.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

from app.services.activitypub import config

logger = logging.getLogger(__name__)

# The reply-addressing block at the very start of a note: a run of mention tokens -- a resolved
# `nostr:npub…` ref (fixed 58-char bech32 body, anchored so it can't swallow a glued word) OR an
# unresolved literal `@handle`. Only a run of 3+ is an addressing WALL to strip -- a reply that opens
# with one or two @names is likely writing to them as content (the p-tags notify everyone regardless).
MENTION_TOKEN = r'(?:nostr:npub1[0-9a-z]{58}|@[A-Za-z0-9_](?:[A-Za-z0-9_.\-]*[A-Za-z0-9_])?(?:@[A-Za-z0-9.\-]+)?)'
LEADING_MENTIONS_RE = re.compile(r'^(?:' + MENTION_TOKEN + r'\s+){3,}', re.I)
# A bare fediverse handle still sitting in the text afterwards. Narrow on purpose: it must not match
# an email address or a `user@host` inside a URL.
PLAIN_HANDLE_RE = re.compile(r'(?<![\w/@.])@[A-Za-z0-9_][A-Za-z0-9_.\-]{0,40}(?:@[A-Za-z0-9.\-]+)?')


async def rewrite(db, port: int, instance_host: str, content: str, mentions: list) -> tuple:
    """(content, [p-tags]) with each mentioned account linked. `mentions` are Mastodon-shaped dicts
    ({url, acct, username}); `instance_host` is the server the note came from (a bare `acct` is local
    to it)."""
    from app.services.fedi_bridge_identity import ensure_puppet
    ptags = []
    # A bare "@bob" is only AMBIGUOUS when two mentioned accounts are both called "bob"; with a single
    # "bob" the note's own mention list settles who it is, whatever server they are on.
    uname_n: dict = {}
    for m in mentions or []:
        u = (m.get("username") or "").strip().lower()
        if u:
            uname_n[u] = uname_n.get(u, 0) + 1
    # Longest acct first so '@user@host' is replaced before a bare '@user' substring.
    for m in sorted(mentions or [], key=lambda x: len(x.get("acct", "")), reverse=True):
        url = (m.get("url") or "").strip()
        acct = (m.get("acct") or m.get("username") or "").strip()
        username = (m.get("username") or "").strip()
        if not url or not acct:
            continue
        # The blocklist gates identities, not only posts: a blocked account must not get a puppet,
        # a profile and a p-tag on a public note purely by being MENTIONED.
        mhost = acct.rsplit("@", 1)[-1].lower() if "@" in acct else (instance_host or "").lower()
        full = acct if "@" in acct else f"{acct}@{mhost}"
        if config.host_blocked(mhost) or config.account_blocked(full):
            continue
        try:
            p = await ensure_puppet(db, port, {"url": url, "acct": acct, "username": username,
                                               "display_name": username},
                                    instance_host, profile_refresh=False)
        except Exception as e:
            # SAY SO: a mention that cannot be provisioned stays dead text for ever and the person is
            # never notified, with no trace anywhere unless this line is written.
            logger.info("[activitypub] mention %s could not be provisioned: %s: %s", acct, type(e).__name__, e)
            p = None
        if not p:
            continue
        ptags.append(["p", p["pubkey_hex"]])
        ref = "nostr:" + p["npub"]
        # Boundary-guarded: a plain replace of "@ann" also matched INSIDE "@anna_x" (and
        # "@bob@other.host" inside "@bob@other.host.evil"), producing a reference no client resolves.
        content = re.sub(r"@" + re.escape(acct) + r"(?![A-Za-z0-9_.\-@])", ref, content)
        # The qualified form of an account LOCAL to the note's server, whose `acct` has no host:
        # a note federated elsewhere renders it "@name@host", which the line above never matches.
        if "@" not in acct:
            for h in {(instance_host or "").lower(), urlparse(url).netloc.lower()}:
                if h:
                    content = re.sub(r"@" + re.escape(f"{acct}@{h}") + r"(?![A-Za-z0-9_.\-@])", ref, content)
        # The BARE "@username": what a note federated from another server usually shows. Claimed when
        # this username is unique in the mention list; otherwise only for the note's own server.
        mhost2 = acct.rsplit("@", 1)[-1].lower() if "@" in acct else ""
        unique = uname_n.get(username.lower(), 0) <= 1
        if username and (unique or not mhost2 or mhost2 == (instance_host or "").lower()):
            content = re.sub(r"@" + re.escape(username) + r"(?![A-Za-z0-9_.\-@])", ref, content)
    leftover = PLAIN_HANDLE_RE.findall(content or "")
    if leftover:
        logger.info("[activitypub] %d handle(s) left as text (%s); the note named %d mention(s)",
                    len(leftover), ", ".join(leftover[:4]), len(mentions or []))
    return content, ptags
