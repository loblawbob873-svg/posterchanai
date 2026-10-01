"""Recipients of NIP-18 quote tags on text notes.

A `q` tag's 4th element (the quoted author) is OPTIONAL in NIP-18, and plenty of clients leave it
out: `["q", "<event id>"]`. Read only from q[3], such a quote addressed nobody — measured, 2 of the 7
quotes of one user's posts were like that, so the person quoted was never told. An event id is the
hash of its author's pubkey, so the author of a quoted event this relay HOLDS is a fact, not a claim:
the store resolves it at ingest (`remember_quote_authors`) and callers that can look events up pass
`resolved` (quoted id → author)."""
import re
from collections import OrderedDict

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_RESOLVED: "OrderedDict[str, frozenset]" = OrderedDict()   # quoting event id → resolved authors
_RESOLVED_MAX = 8192


def _q_tags(ev: dict):
    if ev.get("kind") != 1:
        return
    for t in ev.get("tags") or []:
        if (isinstance(t, (list, tuple)) and len(t) >= 2 and t[0] == "q"
                and isinstance(t[1], str) and _HEX64.fullmatch(t[1])):
            yield t


def _named_author(t) -> str:
    a = t[3] if len(t) >= 4 else None
    return a if isinstance(a, str) and _HEX64.fullmatch(a) else ""


def quoted_ids_without_author(ev: dict) -> list[str]:
    """Ids quoted by `ev` whose q tag names no author — the ones only a lookup can resolve."""
    return list(dict.fromkeys(t[1] for t in _q_tags(ev) if not _named_author(t)))


def remember_quote_authors(eid: str, pubkeys) -> None:
    pks = frozenset(p for p in pubkeys if isinstance(p, str) and _HEX64.fullmatch(p))
    if not eid or not pks:
        return
    _RESOLVED[eid] = pks
    _RESOLVED.move_to_end(eid)
    while len(_RESOLVED) > _RESOLVED_MAX:
        _RESOLVED.popitem(last=False)


def quote_pubkeys(ev: dict, resolved: dict | None = None) -> set[str]:
    out = set()
    for t in _q_tags(ev):
        a = _named_author(t) or ((resolved or {}).get(t[1]) or "")
        if a and _HEX64.fullmatch(a):
            out.add(a)
    if ev.get("kind") == 1:
        out |= _RESOLVED.get(ev.get("id") or "", frozenset())
    return out
