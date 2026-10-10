"""The LIST settings on Admin → Relay, as rows a person can read, add to and remove from.

Relay origins, WoT seeds, GPU-sharing peers, blocked words, blocked bridge domains and the four relay
URL lists were each a textarea: no search, no way to tell a typo from an entry, and -- for the key
lists -- no idea WHO a line is. Like Identities and Blocked accounts they are now drawn as a list, and
every Add/Remove is an edit of the value the SERVER holds right now, never a write-back of what the
page loaded: the same Save-puts-back-a-stale-list bug that undid client-side blocks twice.

One edit goes through `admin.update_settings` -- the exact path Save takes -- so every live reload
(block filters, NIP-05, upstream reconnect, a relay restart for the origins list) fires exactly as it
would for a Save, and is then written through to the operator-signed relay document before the call
answers, so "saved" means it survives a restart.
"""
from __future__ import annotations

import re

# key -> kind. The kind decides how a stored value is split into entries, what a valid new entry is,
# and what a row shows.
LISTS = {
    "nostr_relay_posterchan_origins": "origin",
    "nostr_relay_wot_seeds": "pubkey",
    "nostr_dvm_peers": "peer",
    "nostr_relay_blocked_words": "word",
    "nostr_relay_blocked_relays": "domain",
    "nostr_relay_nip05_relays": "relay",
    "nostr_relay_upstream_relays": "relay",
    "nostr_relay_private_relays": "relay",
    # Admin → Blossom ("fix blossom list textboxes to function like the way you improved the relays in
    # admin"): who may upload (people -- shown with their name and picture), this node's own media hosts,
    # and the servers each upload is mirrored to.
    "blossom_whitelist": "pubkey",
    "media_own_hosts": "domain",
    "blossom_mirror_servers": "server",
    # Admin → Social → Blocking ("make the text area lists like you did for relays"): fediverse instances
    # and single accounts, read exactly the way the blocker reads them (fedi_blocklist.normalize).
    "fedi_bridge_blocked_domains": "fedi",
}

MAX_ENTRY = 500

# Kinds a list FIELD may use that is not a global setting (Admin → Bots → Edit: one bot's own lists, saved
# with the bot). Each is split exactly the way the bot reads it (botframework/*): topics are lines or comma
# pieces (autopost._topics); invites and hosts are whitespace/comma tokens (concord.invites_from_env,
# config.TRUSTED_MEDIA_HOSTS); accounts are tokens (nostrListener NOSTR_RATE_EXEMPT).
FIELD_KINDS = {"pubkey", "word", "domain", "relay", "server", "origin", "topic", "invite", "host"}
_LINES = ("word", "topic")          # an entry may contain spaces; whitespace is not a separator


def same(kind: str, entry: str) -> str:
    """What an entry MEANS, for comparing two entries -- never what is stored or shown.

    Two spellings of one thing are one entry. The upstream list held `wss://nostr.mom/` AND
    `wss://nostr.mom`; they were two rows, so Remove took one, the other was redrawn, and the button
    looked broken ("remove button dont work for RELAY URLS in admin"). The same shape exists in every
    list: a key as npub and as hex, a domain or origin in two cases, a trailing dot or slash."""
    e = " ".join(str(entry or "").split()) if kind not in _LINES else str(entry or "").strip()
    if kind in _LINES:
        return e.lower()
    if kind == "invite":
        return e                     # the part after `#` is the room's key: its case is its meaning
    if kind == "host":
        return e.lower().rstrip(".")
    if kind == "pubkey":
        return _pk(e) or e.lower()
    if kind == "peer":
        toks = e.split()
        if not toks:
            return ""
        relay = toks[1].lower().rstrip("/") if len(toks) > 1 else ""
        return (_pk(toks[0]) or toks[0].lower()) + " " + relay
    if kind == "domain":
        return re.sub(r"^[a-z]+://", "", e, flags=re.I).split("/")[0].strip(".").lower()
    if kind == "fedi":
        return _fedi_key(e)
    return e.lower().rstrip("/")            # relay, origin


def _fedi_key(entry: str) -> str:
    """`https://Bad.Example/`, `*.bad.example`, `@bad.example` and its punycode are one instance; a
    profile link and `user@host` are one account -- what fedi_blocklist enforces, in one spelling."""
    from app.services import fedi_blocklist
    n = fedi_blocklist.normalize(entry)
    user, at, host = n.rpartition("@")
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        pass
    return f"{user}@{host}" if at else host


def entries(kind: str, raw: str) -> list:
    """The entries of a stored value, split the way the relay itself reads it (thread.py): a word or
    phrase is a whole LINE; a peer is a line or comma-separated card; everything else is a token
    separated by whitespace or commas. Order kept, duplicates (case-insensitive) dropped."""
    raw = str(raw or "").replace("\r\n", "\n")
    if kind == "fedi":
        from app.services import fedi_blocklist
        parts = fedi_blocklist.tokens(raw)
    elif kind == "word":
        parts = [ln.strip() for ln in raw.split("\n")]
    elif kind == "topic":
        parts = [t.strip() for ln in raw.split("\n") for t in ln.split(",")]
    elif kind == "peer":
        parts = [" ".join(ln.split()) for ln in raw.replace(",", "\n").split("\n")]
    else:
        parts = raw.replace(",", " ").split()
    out, seen = [], set()
    for p in parts:
        k = same(kind, p) if p else ""
        if p and k not in seen:
            seen.add(k)
            out.append(p)
    return out


def _pk(tok: str) -> str:
    from app.services.nostr import nostr_service
    try:
        return nostr_service.to_pubkey_hex((tok or "").strip()) or ""
    except Exception:
        return ""


def validate(kind: str, entry: str):
    """(clean entry, None) or (None, reason) for a NEW entry typed into the Add box."""
    e = " ".join(str(entry or "").split()) if kind not in _LINES else str(entry or "").strip()
    if not e:
        return None, "empty"
    if len(e) > MAX_ENTRY and kind != "invite":     # an invite carries a whole naddr: long by nature
        return None, "too long"
    if kind == "word":
        if "\n" in e:
            return None, "one word or phrase at a time"
        return e, None
    if kind == "topic":
        if "\n" in e or "," in e:
            return None, "one topic at a time (a comma would split it in two)"
        return e, None
    if kind != "peer" and " " in e:
        return None, "one entry at a time (no spaces)"
    if kind == "pubkey":
        return (e, None) if _pk(e) else (None, "not an npub or 64-character hex key")
    if kind == "peer":
        toks = e.split()
        if len(toks) != 2 or not _pk(toks[0]):
            return None, "a peer is: npub relay-url"
        if not re.match(r"^wss?://[^\s/]+", toks[1], re.I):
            return None, "the relay URL must start with wss:// (or ws://)"
        return e, None
    if kind == "relay":
        return (e, None) if re.match(r"^wss?://[^\s/]+", e, re.I) else (None, "a relay URL starts with wss:// (or ws://)")
    if kind == "server":
        # blossom_service reads only http(s) URLs out of this list and drops anything else silently.
        return (e.rstrip("/"), None) if re.match(r"^https?://[^\s/]+", e, re.I) else (None, "a server URL starts with https:// (or http://)")
    if kind == "origin":
        m = re.match(r"^([a-z][a-z0-9+.-]*)://([^\s/]+)/?$", e, re.I)
        return (m.group(0).rstrip("/"), None) if m else (None, "an origin is scheme://host[:port], e.g. https://poster.place")
    if kind == "fedi":
        from app.services import fedi_blocklist
        n = fedi_blocklist.normalize(e)
        host = n.rpartition("@")[2]
        if not n or not re.match(r"^[^\s@/]+\.[^\s@/.]+$", host):
            return None, "not an instance (bad.example) or an account (someone@bad.example)"
        return n, None
    if kind == "invite":
        if not re.match(r"^https?://[^\s/]+/\S*#\S+$", e, re.I):
            return None, "not a Concord invite link (https://…/c/naddr1…#…)"
        return e, None
    if kind == "host":
        h = e.lower().rstrip(".")
        if not re.match(r"^(\[[0-9a-f:]+\]|[a-z0-9_.-]+)(:\d{1,5})?$", h):
            return None, "a host name or IP address (nas.lan, 192.168.0.85)"
        return h, None
    if kind == "domain":
        d = re.sub(r"^[a-z]+://", "", e, flags=re.I).split("/")[0].strip(".").lower()
        return (d, None) if re.match(r"^[a-z0-9.-]+\.[a-z0-9-]+$", d) else (None, "not a domain name")
    return None, "unknown list"


def edit(raw: str, kind: str, add: str = "", remove: str = ""):
    """(new value, error) after adding and/or removing ONE entry. Removal matches case-insensitively
    on the whole entry. The value is rewritten one entry per line."""
    # EVERY stored spelling, not the de-duplicated view: a remove must take all of them, or the one
    # left behind is drawn again and the entry "comes back".
    raw_parts = str(raw or "").replace("\r\n", "\n")
    if kind == "fedi":
        return _edit_fedi(raw_parts, add, remove)
    if kind == "word":
        stored = [ln.strip() for ln in raw_parts.split("\n") if ln.strip()]
    elif kind == "topic":
        stored = [t.strip() for ln in raw_parts.split("\n") for t in ln.split(",") if t.strip()]
    elif kind == "peer":
        stored = [" ".join(ln.split()) for ln in raw_parts.replace(",", "\n").split("\n") if ln.strip()]
    else:
        stored = raw_parts.replace(",", " ").split()
    if remove:
        want = same(kind, remove)
        kept = [e for e in stored if same(kind, e) != want]
        if len(kept) == len(stored):
            return None, "not in the list (it may have changed — reload)"
        stored = kept
    cur = entries(kind, "\n".join(stored))
    if add:
        clean, err = validate(kind, add)
        if err:
            return None, err
        if kind in ("relay", "server"):
            clean = clean.rstrip("/")
        if same(kind, clean) in {same(kind, e) for e in cur}:
            return None, "already in the list"
        cur.append(clean)
    return "\n".join(cur), None


def _edit_fedi(raw: str, add: str, remove: str):
    """The fediverse blocklist is typed by hand for years and carries `# why` notes; an edit keeps every
    comment and every other line as it was, takes out the entry (in every spelling) and appends a new one."""
    lines = raw.split("\n")
    if remove:
        want = _fedi_key(remove)
        hit, out = False, []
        for ln in lines:
            body, hash_, note = ln.partition("#")
            toks = body.replace(",", " ").split()
            kept = [t for t in toks if _fedi_key(t) != want]
            if len(kept) == len(toks):
                out.append(ln)
                continue
            hit = True
            if kept or hash_:
                out.append((" ".join(kept) + (" " if kept and hash_ else "") + (hash_ + note if hash_ else "")).rstrip())
        if not hit:
            return None, "not in the list (it may have changed — reload)"
        lines = out
    if add:
        clean, err = validate("fedi", add)
        if err:
            return None, err
        from app.services import fedi_blocklist
        if _fedi_key(clean) in {_fedi_key(t) for t in fedi_blocklist.tokens("\n".join(lines))}:
            return None, "already in the list"
        while lines and not lines[-1].strip():
            lines.pop()
        lines.append(clean)
    return "\n".join(ln for ln in lines).strip("\n"), None


async def rows(key: str, raw: str) -> dict:
    """The rows for one list; key-bearing kinds carry the owner's name/picture from this relay."""
    out = await rows_of(LISTS[key], raw)
    out["key"] = key
    return out


async def rows_of(kind: str, raw: str) -> dict:
    """The rows for a value of any list KIND -- a global setting's, or a field the page holds."""
    from app.services import relay_blocklist
    from app.services.nostr import nostr_service
    items = entries(kind, raw)
    out, names_ok = [], True
    if kind in ("pubkey", "peer"):
        pks = {}
        for e in items:
            pk = _pk(e.split()[0])
            if pk:
                pks[e] = pk
        found, names_ok = await relay_blocklist.profiles(sorted(set(pks.values()))) if pks else ({}, True)
        for e in items:
            pk = pks.get(e, "")
            p = found.get(pk) or {}
            try:
                npub = nostr_service.npub_of(pk) if pk else ""
            except Exception:
                npub = pk
            out.append({"value": e, "pubkey": pk, "npub": npub, "name": p.get("name", ""),
                        "picture": p.get("picture", ""), "nip05": p.get("nip05", ""),
                        "relay": e.split()[1] if kind == "peer" and len(e.split()) > 1 else "",
                        "valid": bool(pk)})
    else:
        for e in items:
            row = {"value": e, "valid": validate(kind, e)[1] is None}
            if kind == "fedi":
                row["type"] = "account" if "@" in _fedi_key(e) else "instance"
            if kind == "invite":      # never draw the room key; what precedes `#` says which room it is
                row["shown"] = e.split("#", 1)[0] + "#••••••"
            out.append(row)
    return {"kind": kind, "items": out, "names_complete": names_ok}
