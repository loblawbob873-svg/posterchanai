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
}

MAX_ENTRY = 500


def same(kind: str, entry: str) -> str:
    """What an entry MEANS, for comparing two entries -- never what is stored or shown.

    Two spellings of one thing are one entry. The upstream list held `wss://nostr.mom/` AND
    `wss://nostr.mom`; they were two rows, so Remove took one, the other was redrawn, and the button
    looked broken ("remove button dont work for RELAY URLS in admin"). The same shape exists in every
    list: a key as npub and as hex, a domain or origin in two cases, a trailing dot or slash."""
    e = " ".join(str(entry or "").split()) if kind != "word" else str(entry or "").strip()
    if kind == "word":
        return e.lower()
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
    return e.lower().rstrip("/")            # relay, origin


def entries(kind: str, raw: str) -> list:
    """The entries of a stored value, split the way the relay itself reads it (thread.py): a word or
    phrase is a whole LINE; a peer is a line or comma-separated card; everything else is a token
    separated by whitespace or commas. Order kept, duplicates (case-insensitive) dropped."""
    raw = str(raw or "").replace("\r\n", "\n")
    if kind == "word":
        parts = [ln.strip() for ln in raw.split("\n")]
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
    e = " ".join(str(entry or "").split()) if kind != "word" else str(entry or "").strip()
    if not e:
        return None, "empty"
    if len(e) > MAX_ENTRY:
        return None, "too long"
    if kind == "word":
        if "\n" in e:
            return None, "one word or phrase at a time"
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
    if kind == "origin":
        m = re.match(r"^([a-z][a-z0-9+.-]*)://([^\s/]+)/?$", e, re.I)
        return (m.group(0).rstrip("/"), None) if m else (None, "an origin is scheme://host[:port], e.g. https://poster.place")
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
    if kind == "word":
        stored = [ln.strip() for ln in raw_parts.split("\n") if ln.strip()]
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
        if kind == "relay":
            clean = clean.rstrip("/")
        if same(kind, clean) in {same(kind, e) for e in cur}:
            return None, "already in the list"
        cur.append(clean)
    return "\n".join(cur), None


async def rows(key: str, raw: str) -> dict:
    """The rows for one list; key-bearing kinds carry the owner's name/picture from this relay."""
    from app.services import relay_blocklist
    from app.services.nostr import nostr_service
    kind = LISTS[key]
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
            out.append({"value": e, "valid": validate(kind, e)[1] is None})
    return {"key": key, "kind": kind, "items": out, "names_complete": names_ok}
