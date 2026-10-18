"""A REPOST OF A BLOCKED AUTHOR IS THAT AUTHOR'S POST: one rule for every way an event reaches the relay.

A kind-6/16 repost carries the reposted event in its content, so a member reposting a blocked account put that
account's post in front of every client ("how is The_Toddstr's posts still making it to our service despite being
blocked", 2026-10-17). The direct-write path refused them; the live firehose, the sync sweep and the thread-ancestor
backfill did not (security review 2026-10-18), so a repost arriving from upstream was stored and served until the next
block purge -- and that purge finds them by `p` tag, so one naming the author only in its content stayed for good.
"""
import json

_REPOST_KINDS = (6, 16)


def reposted_authors(ev) -> set:
    """Who a kind-6/16 repost reposts: the embedded event's author (its content is that event's JSON) and every
    `p` it names (NIP-18 tags the reposted author). Empty for any other kind."""
    if not isinstance(ev, dict) or ev.get("kind") not in _REPOST_KINDS:
        return set()
    out = {t[1] for t in ev.get("tags") or () if isinstance(t, list) and len(t) >= 2 and t[0] == "p"
           and isinstance(t[1], str)}
    c = ev.get("content") or ""
    if isinstance(c, str) and c.startswith("{"):
        try:
            inner = json.loads(c)
            if isinstance(inner, dict) and isinstance(inner.get("pubkey"), str):
                out.add(inner["pubkey"])
        except ValueError:
            pass
    return out


def reposted_blocked(ev, is_blocked) -> bool:
    return any(is_blocked(pk) for pk in reposted_authors(ev))
