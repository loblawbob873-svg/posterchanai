"""Spread web searches over the nodes in Site → Load Balancing, the way chat/image/music/video are.

WHY A SEARCH IS WORTH BALANCING, even though it costs no GPU. The expensive, rationed resource here
is not compute, it is **an IP address that search engines still answer**. One node makes every query
from one address; engines rate-limit that address, and SearXNG's response to an engine that answers
with a 429 or a CAPTCHA is to SUSPEND it for an hour — which reaches the user as "no results", not as
a rate limit. Two nodes are two addresses and two engine-suspension states, so a search that would
have been throttled is simply made from somewhere else.

That is also why this composes with `searxng_proxy_engines` rather than competing with it: the proxy
changes WHICH address a node's engine requests leave from (Tor1 → Tor2 → direct), and this changes
WHICH NODE asks. A Tor exit is shared with strangers and gets blocked; a peer node is not.

THE PEER RUNS ITS OWN SEARCH, END TO END. `/api/search` on the far side calls that node's
`web_search_local`, which resolves ITS OWN instance (`resolve_searxng_url`: its admin setting → its
bundled SearXNG → the public fallback) and uses ITS OWN outgoing proxy. This mirrors
`music_factory`/`image_factory`, where the remote node runs its own local path rather than being told
how to do the work — settings are per-node, and a node that is told which instance to use would
search through a box it may not even be able to reach.

**`web_search_local` NEVER forwards.** That is the whole loop guard: the peer endpoint calls the
local half only, so A→B cannot become B→A→B. Nothing else is needed and nothing else would be
reliable — a hop counter in a header is set by the caller.

AN EMPTY ANSWER IS NOT A FAILURE, BUT IT IS WORTH ASKING TWICE. A node whose engines are all
suspended returns `[]`, and so does a genuinely obscure query; from here they are identical. So an
empty result set is retried ONCE on the next node and kept only if that node did better — the same
rule `runSearch` in the client already applies to an incomplete relay answer, for the same reason.
An EXCEPTION is different and always fails over: the node did not answer at all.
"""
from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

import httpx

from app.services import settings_store
from app.utils import lb_auth

logger = logging.getLogger(__name__)

# The sentinel for "this node", so the local path takes its turn in the rotation like any peer
# instead of always going first (which would starve the peers and defeat the point).
_LOCAL = "__local__"

_rr_index = 0
_rr_lock = asyncio.Lock()


def enabled() -> bool:
    return settings_store.get_bool("searxng_load_balance", True)


def parse_search_server_urls(raw: str) -> List[str]:
    """The unified node list, EXCLUDING this node — it is already represented by `_LOCAL`, and
    leaving its own URL in would forward a search to itself over HTTP and starve real peers."""
    if not raw:
        return []
    from app.services.load_balancer import parse_server_urls
    return parse_server_urls(raw, exclude_self=True)


def peers() -> List[str]:
    return parse_search_server_urls(settings_store.get("chat_server_urls", "") or "")


async def _rotated(candidates: List[str]) -> List[str]:
    """`candidates` rotated by a global round-robin index, so each search starts at a different node.

    The index advances by 1 modulo a large constant rather than `% len(candidates)` — the same
    reasoning as music_factory: a single-candidate call would otherwise reset it to 0 and every
    search would start at the same node. A single-candidate call does not advance it at all, because
    it is not a balancing decision.
    """
    global _rr_index
    if not candidates:
        return []
    async with _rr_lock:
        start = _rr_index % len(candidates)
        if len(candidates) > 1:
            _rr_index = (_rr_index + 1) % 1_000_000
    return candidates[start:] + candidates[:start]


async def _search_on_node(node_url: str, payload: dict, timeout: float) -> List[dict]:
    """Ask a peer node to run the search on its own instance. Raises on anything but a clean answer."""
    url = node_url.rstrip("/") + "/api/search"
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
        r = await client.post(url, json=payload, headers=lb_auth.headers())
        r.raise_for_status()
        data = r.json()
    results = data.get("results")
    if not isinstance(results, list):
        raise ValueError("peer returned no results array")
    return results


async def web_search(
    service,
    query: str,
    limit: int = 5,
    categories: Optional[str] = None,
    time_range: Optional[str] = None,
    sort_recent: bool = False,
) -> List[dict]:
    """Run `query` on this node or a peer, whichever the rotation picks. Never raises.

    `service` is the SearchService whose `web_search_local` does the work here — passed in rather
    than constructed so the caller's DB session and settings are the ones used.
    """
    payload = {
        "query": query, "limit": limit, "categories": categories,
        "time_range": time_range, "sort_recent": sort_recent,
    }
    timeout = float(settings_store.get_int("search_lb_timeout", 45))

    candidates: List[str] = [_LOCAL]
    if enabled():
        try:
            candidates += peers()
        except Exception as exc:                       # a bad node list must not stop this node
            logger.warning("[search-lb] could not read the node list: %s", exc)
    order = await _rotated(candidates)

    empty_from: Optional[str] = None                   # a node that ANSWERED, with nothing
    for node in order:
        try:
            if node == _LOCAL:
                results = await service.web_search_local(
                    query, limit=limit, categories=categories,
                    time_range=time_range, sort_recent=sort_recent)
            else:
                results = await _search_on_node(node, payload, timeout)
        except Exception as exc:
            logger.warning("[search-lb] %s failed: %s", "local" if node == _LOCAL else node, exc)
            continue
        if results:
            return results
        # Answered with nothing. Keep looking, but only far enough to tell "obscure query" from
        # "this node's engines are suspended" — the first empty is remembered so a second empty is
        # returned as the honest answer rather than as a failure.
        if empty_from is not None:
            return []
        empty_from = node

    return []
