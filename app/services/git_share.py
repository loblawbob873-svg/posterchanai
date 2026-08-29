"""Public, shareable git repo pages — owner resolution + the OpenGraph card a link renders as.

Two jobs, both about a repo link that leaves this app:

1. **Owner resolution.** A shareable repo URL is `/r/<owner>/<repo-id>`, and `<owner>` is whatever a
   person can actually type or paste: an `npub`, a raw hex pubkey, or a **NIP-05 name granted on this
   node** (`/r/alice/my-app`). One resolver, so the server route and the client route agree on what a
   URL means.

2. **The card.** A repo announcement (NIP-34 kind 30617) already carries a name, a description, a
   clone URL and an author — everything a link preview needs. Nothing was reading it, so every repo
   link ever shared (into Nostr, Telegram, Slack, a group chat) rendered as a bare URL with the
   generic app title, which is indistinguishable from a broken link. This reads the announcement off
   THIS node's relay and hands back the meta tags.

The read is best-effort and short-cached: a preview that cannot be built must degrade to the app's
own title, never to an error page — a crawler that gets a 500 renders nothing at all, and the human
who clicks the link would still have reached a working repo.
"""

import logging
import time

logger = logging.getLogger(__name__)

ANNOUNCE_KIND = 30617
STATE_KIND = 30618

# repo id allowlist — the SAME shape the git host accepts (git_host_service), so a URL that resolves
# here can never name a repo the host would refuse.
_ID_MAX = 100

_CARD_TTL = 300.0        # 5 min: an announcement is replaceable but rarely changes; a crawler storm
#                          on one shared link must not become one relay query per hit.
_cache: dict = {}


def valid_repo_id(repo_id: str) -> bool:
    """`^[a-z0-9][a-z0-9._-]{0,99}$`, mirroring the git host's own allowlist."""
    import re
    return bool(repo_id) and bool(re.match(r"^[a-z0-9][a-z0-9._-]{0,%d}$" % (_ID_MAX - 1), repo_id))


def valid_url_repo_id(repo_id: str) -> bool:
    """The bound on the `<repo>` segment of a shared URL — the SAME shape, case-INSENSITIVE.

    A repo id minted by this host is lowercase, but the page also serves repos announced from
    anywhere, and NIP-34 does not say a `d` tag must be lowercase. Folding the case here would make
    `/r/<npub>/MyApp` look up an announcement tagged `myapp` and find nothing, so the page previews
    nothing for exactly the repos that are not ours."""
    import re
    return bool(repo_id) and bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,%d}$" % (_ID_MAX - 1), repo_id))


def resolve_owner(owner: str) -> str | None:
    """`<owner>` from a repo URL -> a 64-char hex pubkey, or None.

    Accepts an npub, a raw hex pubkey, or a NIP-05 name this node has granted. The nip05 branch is
    LAST and only consulted for something that is not already a key, so a valid npub can never be
    shadowed by a name that happens to look like one.
    """
    owner = (owner or "").strip()
    if not owner:
        return None
    from app.services.nostr import nostr_service
    try:
        hexed = nostr_service.to_pubkey_hex(owner)
    except Exception:
        hexed = None
    if hexed:
        # LOWERCASED here, not assumed: `to_pubkey_hex` returns an already-hex input verbatim, so a
        # link written with an uppercase key would be carried straight into a relay filter, where
        # `authors` is matched byte-for-byte — an exact-case miss that reads as "this repo has no
        # announcement" rather than as a malformed URL.
        return hexed.lower()
    # A local NIP-05 name. Names are granted in one setting, so this is a dict lookup, not a fetch —
    # and deliberately only OUR names: resolving a foreign name@domain here would make this node
    # answer for a repo it has no announcement for.
    # A qualified NIP-05 address must name THIS instance.  Dropping the domain here would make
    # `alice@evil.example` resolve to a locally granted `alice`, silently changing whose repository
    # the shared URL opens.  Bare names remain useful for the short local form (`/r/alice/repo`).
    from app.services import settings_store
    if "@" in owner:
        if owner.count("@") != 1:
            return None
        local, supplied_domain = owner.rsplit("@", 1)
        supplied_domain = supplied_domain.strip().lower().rstrip(".")
        configured_domain = (settings_store.get("nostr_relay_nip05_domain", "") or "").strip()
        configured_domain = configured_domain.lstrip("@").lower().rstrip(".")
        if not supplied_domain or not configured_domain or supplied_domain != configured_domain:
            return None
        name = local.strip().lower()
    else:
        name = owner.lower()
    if not name or len(name) > 64:
        return None
    try:
        from app.services.nostr_relay.thread import _parse_nip05
        names, _ = _parse_nip05(settings_store.get("nostr_relay_nip05_names", "") or "", "")
        for n, hx in (names or {}).items():
            if (n or "").lower() == name:
                # Lowercased for the same reason the npub branch above is: this value goes into a
                # relay `authors` filter, which matches byte-for-byte. A settings line written with
                # an uppercase key would make every lookup for that owner miss.
                return (hx or "").lower() or None
    except Exception as e:
        logger.debug("[git-share] nip05 owner lookup failed for %r: %s", owner, e)
    return None


def _dtag(ev: dict) -> str:
    return _tag(ev, "d")


def _tag(ev: dict, key: str) -> str:
    for t in (ev.get("tags") or []):
        if len(t) >= 2 and t[0] == key and t[1]:
            return str(t[1])
    return ""


def _tag_values(ev: dict, key: str) -> list:
    for t in (ev.get("tags") or []):
        if len(t) >= 2 and t[0] == key:
            return [str(v) for v in t[1:] if v]
    return []


async def repo_card(port: int, owner_hex: str, repo_id: str) -> dict | None:
    """The shareable facts about one repo, read off this node's relay. None when there is no
    announcement (an unannounced or private repo has no public page to preview)."""
    key = "%s/%s" % (owner_hex, repo_id)
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    card = None
    try:
        from app.services import nostr_store
        # BOTH SPELLINGS IN ONE FILTER. `#d` is matched byte-for-byte, and the identifier can reach
        # us either way: our own links carry the `d` tag verbatim, while a person retyping a URL
        # lowercases it by habit. Asking for both costs one query and means neither spelling is a
        # silent miss. (Folding to one would break the other; there is no canonical case here.)
        want = [repo_id] if repo_id == repo_id.lower() else [repo_id, repo_id.lower()]
        # strict=True, or a DEAD RELAY is indistinguishable from a repo with no announcement:
        # _ws_query swallows a timeout and returns [], which the negative cache below would then
        # pin for its whole TTL. Unreachable must RAISE so the read is not cached at all.
        evs = await nostr_store._ws_query(port, [{
            "authors": [owner_hex], "kinds": [ANNOUNCE_KIND], "#d": want, "limit": 4}],
            timeout=4.0, strict=True)
        # Prefer an exact-case match over a folded one, then the newest.
        ann = sorted(evs or [], key=lambda e: (_dtag(e) == repo_id, e.get("created_at") or 0),
                     reverse=True)
        if ann:
            e = ann[0]
            card = {
                "owner": owner_hex,
                "repo": repo_id,
                "name": _tag(e, "name") or repo_id,
                "description": _tag(e, "description") or (e.get("content") or "").strip(),
                "clone": (_tag_values(e, "clone") or [""])[0],
                "web": (_tag_values(e, "web") or [""])[0],
                "updated": int(e.get("created_at") or 0),
                "maintainers": _tag_values(e, "maintainers"),
                "image": "",
                "author": "",
            }
            # The author's avatar + display name make the card look like a project card rather than a
            # URL. Best-effort and never fatal: a missing kind 0 just leaves the node's own branding.
            try:
                metas = await nostr_store._ws_query(port, [{
                    "authors": [owner_hex], "kinds": [0], "limit": 1}], timeout=3.0)
                if metas:
                    import json as _json
                    prof = _json.loads(metas[0].get("content") or "{}")
                    if isinstance(prof, dict):
                        pic = (prof.get("picture") or "").strip()
                        if pic.startswith(("http://", "https://")):
                            card["image"] = pic
                        card["author"] = (prof.get("display_name") or prof.get("name") or "").strip()
            except Exception:
                pass
    except Exception as e:
        logger.debug("[git-share] card read failed for %s: %s", key, e)
        return None      # NOT cached: a relay hiccup must not pin a repo to "no preview" for 5 min
    _cache[key] = (now + _CARD_TTL, card)
    if len(_cache) > 2000:
        _cache.clear()
    return card


def og_meta(card: dict, url: str, fallback_image: str = "") -> dict:
    """A repo card -> the `meta` dict the client shell renders into <head>."""
    name = (card.get("name") or card.get("repo") or "repo").strip()
    who = (card.get("author") or "").strip()
    title = "%s · %s" % (name, who) if who else name
    desc = (card.get("description") or "").strip()
    if not desc:
        desc = "A git repository on Nostr — browse the code, history and issues."
    return {
        "title": title[:120],
        "description": desc[:300],
        "url": url,
        "image": (card.get("image") or fallback_image or ""),
        "type": "object",
    }
