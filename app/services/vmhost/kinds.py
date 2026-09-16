"""The VM-hosting wire kinds and the relay's rules for carrying them.

Pure functions, no imports beyond the standard library: the relay subprocess (`nostr_relay/server.py`
write gate and `nostr_relay/thread.py` firehose) and the app both use exactly these, so the relay and
the host service cannot disagree about what a request looks like.

    5310  request   client → host   NIP-44, p=<host>, expiration ≤ now+120 (≤600 accepted), nofederate
    6310  result    host → client   NIP-44, e=<request>, p=<requester>, expiration, nofederate
    7310  progress  host → client   same shape as a result, zero or more before it
    31310 announce  host            public, addressable, d=posterchan-vmhost

WHY THE RELAY NEEDS ITS OWN RULE AT ALL. A requester is almost never in this relay's web of trust —
the whole point is that an admin's npub, or somebody an admin assigned a VM to, can reach the host
with nothing but a key and relays. Left to the WoT gate, every request from such a person is refused
`blocked: not in web of trust` and the host never hears it: the feature does not work, and nothing on
the host's side says so. The node-agent transport lost a round of debugging to exactly this (its
kinds were missing from three hardcoded lists), so these kinds get an explicit branch in both ingest
paths and a test that drives the shipped code.

WHAT KEEPS IT FROM BEING AN OPEN PIPE: a stranger's request is accepted only when it is addressed to
THIS node, is short-lived (expiration present and near), carries `nofederate`, and is small. It is
stored briefly (the expiration sweep removes it) and never broadcast. The host then decrypts only
after an allowlist check — a stranger costs one stored row and nothing else.
"""
from __future__ import annotations

REQ_KIND = 5310
RES_KIND = 6310
PROGRESS_KIND = 7310
ANNOUNCE_KIND = 31310
ANNOUNCE_D = "posterchan-vmhost"

TRANSPORT_KINDS = (REQ_KIND, RES_KIND, PROGRESS_KIND)
ALL_KINDS = TRANSPORT_KINDS + (ANNOUNCE_KIND,)

# A request asks for a few seconds of a host's attention; ten minutes is the most the relay stores
# one for, and the host itself refuses anything older than REQ_MAX_AGE.
REQ_MAX_EXPIRATION = 600
RES_MAX_EXPIRATION = 3600
MAX_CONTENT = 70_000          # NIP-44 base64 of a ≤65KB plaintext, plus padding
MAX_ANNOUNCE_CONTENT = 8_000


def is_vmhost_kind(kind) -> bool:
    try:
        return int(kind) in ALL_KINDS
    except (TypeError, ValueError):
        return False


def _tags(ev: dict) -> list:
    t = ev.get("tags")
    return t if isinstance(t, list) else []


def tag_values(ev: dict, name: str) -> list:
    return [t[1] for t in _tags(ev) if isinstance(t, list) and len(t) >= 2 and t[0] == name
            and isinstance(t[1], str)]


def has_tag(ev: dict, name: str) -> bool:
    return any(isinstance(t, list) and t and t[0] == name for t in _tags(ev))


def expiration_of(ev: dict):
    vals = tag_values(ev, "expiration")
    if not vals:
        return None
    try:
        return int(vals[0])
    except (TypeError, ValueError):
        return None


def _shape_refusal(ev: dict, now: float, max_exp: int, max_content: int) -> str | None:
    exp = expiration_of(ev)
    if exp is None:
        return "invalid: vm hosting events must carry an expiration"
    if exp <= int(now):
        return "invalid: event has expired"
    if exp > int(now) + max_exp:
        return "invalid: vm hosting expiration too far in the future"
    if not has_tag(ev, "nofederate"):
        return "invalid: vm hosting events must carry nofederate"
    if len(str(ev.get("content", ""))) > max_content:
        return "invalid: vm hosting payload too large"
    return None


def write_refusal(ev: dict, *, node_pubkey: str | None, is_member, is_operator,
                  wot_enabled: bool = True, now: float) -> str | None:
    """None = accept this event on the WS write path; otherwise the NIP-01 OK reason to refuse it.

    `is_member` / `is_operator` are the relay gate's own predicates."""
    try:
        kind = int(ev.get("kind", -1))
    except (TypeError, ValueError):
        return "invalid: bad kind"
    author = ev.get("pubkey", "")
    if kind == ANNOUNCE_KIND:
        if tag_values(ev, "d")[:1] != [ANNOUNCE_D]:
            # Some other application's 31310 is none of our business: it gets the ordinary gate.
            return None if (not wot_enabled or is_member(author)) else "blocked: not in web of trust"
        if len(str(ev.get("content", ""))) > MAX_ANNOUNCE_CONTENT:
            return "invalid: vm host announcement too large"
        return None
    if kind == REQ_KIND:
        why = _shape_refusal(ev, now, REQ_MAX_EXPIRATION, MAX_CONTENT)
        if why:
            return why
        to_me = bool(node_pubkey) and node_pubkey in tag_values(ev, "p")
        if to_me or not wot_enabled or is_member(author):
            return None
        return "blocked: vm request not addressed to this host"
    if kind in (RES_KIND, PROGRESS_KIND):
        why = _shape_refusal(ev, now, RES_MAX_EXPIRATION, MAX_CONTENT)
        if why:
            return why
        if not wot_enabled or is_member(author):
            return None
        for p in tag_values(ev, "p"):
            if (node_pubkey and p == node_pubkey) or is_member(p) or is_operator(p):
                return None
        return "blocked: vm result not for this relay's users"
    return None


def firehose_accept(ev: dict, *, node_pubkey: str | None, is_member, is_operator, now: float) -> bool:
    """The live firehose's version of the same rule: a READ of an upstream relay, so it only keeps
    what is addressed to this node (or, for results, to one of its users)."""
    try:
        kind = int(ev.get("kind", -1))
    except (TypeError, ValueError):
        return False
    if kind == ANNOUNCE_KIND:
        return tag_values(ev, "d")[:1] == [ANNOUNCE_D] and \
            len(str(ev.get("content", ""))) <= MAX_ANNOUNCE_CONTENT
    if kind == REQ_KIND:
        if _shape_refusal(ev, now, REQ_MAX_EXPIRATION, MAX_CONTENT):
            return False
        return bool(node_pubkey) and node_pubkey in tag_values(ev, "p")
    if kind in (RES_KIND, PROGRESS_KIND):
        if _shape_refusal(ev, now, RES_MAX_EXPIRATION, MAX_CONTENT):
            return False
        return any((node_pubkey and p == node_pubkey) or is_member(p) or is_operator(p)
                   for p in tag_values(ev, "p"))
    return False


def firehose_kinds(enabled: bool) -> list:
    """What the firehose's targeted `#p` subscription asks upstream for (empty = no subscription)."""
    return list(TRANSPORT_KINDS) if enabled else []
