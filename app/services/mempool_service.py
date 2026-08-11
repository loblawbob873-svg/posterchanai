"""mempool.space blocks for the desktop widget — proxied and cached by this node.

WHY THE SERVER FETCHES IT, the same reason as weather: the client could call mempool.space directly,
it is free and needs no key, but then every reader's IP goes to a third party on a timer. One
node-side fetch, cached and shared, means the upstream sees this server and nothing else — and a
client behind Tor keeps its circuit instead of a browser fetch that would leave through it anyway.

It is also the only way this stays cheap. A widget on twenty desktops is twenty pollers; behind a
30-second cache it is two requests a minute from the node however many people have it open.

WHAT IS RETURNED IS NOT WHAT IS FETCHED. The upstream answers are large — a confirmed block carries
its full extras block, a pool object, fee histograms — and a widget draws four numbers per block. The
payload is reduced HERE so the wire carries a few hundred bytes rather than a few hundred KB, twenty
times a minute, to a screen that is showing eight tiles.

`mempool_api_base` lets an operator point this at their OWN mempool instance, which is the whole
point of self-hosting one; the default is the public site.
"""
from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)

DEFAULT_BASE = "https://mempool.space"
_TIMEOUT = 12.0
# Bitcoin blocks arrive every ten minutes and the projected ones re-shuffle continuously; 30s is
# frequent enough that a new block appears while you are looking, and slow enough that a desk full of
# these costs the node two requests a minute.
_TTL = 30.0

_cache: dict = {}        # base -> (at, payload)
_lock = asyncio.Lock()


def _base() -> str:
    try:
        from app.services import settings_store
        v = (settings_store.get("mempool_api_base") or "").strip()
    except Exception:
        v = ""
    return (v or DEFAULT_BASE).rstrip("/")


async def _get_json(url: str):
    import httpx
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            r = await client.get(url, headers={"Accept": "application/json"})
        if r.status_code != 200:
            logger.warning("[mempool] %s HTTP %s", url, r.status_code)
            return None
        return r.json()
    except Exception as e:
        logger.warning("[mempool] %s failed: %s", url, e)
        return None


def _num(v, d=0):
    try:
        n = float(v)
        return n if n == n and n not in (float("inf"), float("-inf")) else d
    except Exception:
        return d


async def blocks() -> dict:
    """Projected (pending) and recent confirmed blocks, reduced to what a widget draws.

    Never raises, and never answers with nothing when it has something: a failed fetch serves the last
    good payload marked `stale`, because a block explorer that blanks on a blip is less use than one
    that is thirty seconds behind and says so.
    """
    base = _base()
    now = time.time()
    hit = _cache.get(base)
    if hit and (now - hit[0]) < _TTL:
        return hit[1]

    async with _lock:
        hit = _cache.get(base)
        if hit and (time.time() - hit[0]) < _TTL:
            return hit[1]

        # CONCURRENTLY. Serially this is two round trips to the same host before anything is drawn,
        # and the widget's whole job is to be glanceable.
        pend, conf = await asyncio.gather(
            _get_json(f"{base}/api/v1/fees/mempool-blocks"),
            _get_json(f"{base}/api/v1/blocks"),
        )
        if not isinstance(pend, list) and not isinstance(conf, list):
            return dict(hit[1], stale=True) if hit else {"ok": False, "pending": [], "blocks": []}

        pending = []
        for b in (pend if isinstance(pend, list) else [])[:3]:
            if not isinstance(b, dict):
                continue
            rng = [r for r in (b.get("feeRange") or []) if isinstance(r, (int, float))]
            pending.append({
                "median": round(_num(b.get("medianFee")), 1),
                # The RANGE is what the tile shows as "n–m sat/vB"; upstream sends a whole histogram
                # and the ends are the only part that fits on a tile.
                "lo": round(_num(rng[0] if rng else b.get("medianFee")), 1),
                "hi": round(_num(rng[-1] if rng else b.get("medianFee")), 1),
                "tx": int(_num(b.get("nTx"))),
                "vsize": int(_num(b.get("blockVSize"))),
            })

        out = []
        for b in (conf if isinstance(conf, list) else [])[:4]:
            if not isinstance(b, dict):
                continue
            ex = b.get("extras") or {}
            pool = (ex.get("pool") or {}) if isinstance(ex, dict) else {}
            out.append({
                "height": int(_num(b.get("height"))),
                "ts": int(_num(b.get("timestamp"))),
                "tx": int(_num(b.get("tx_count"))),
                "median": round(_num(ex.get("medianFee")), 1) if isinstance(ex, dict) else 0,
                "pool": str(pool.get("name") or "")[:24],
            })

        payload = {"ok": True, "pending": pending, "blocks": out, "at": int(time.time())}
        _cache[base] = (time.time(), payload)
        # Bounded for form's sake: the key is a SETTING, not caller input, so this holds one entry in
        # practice — but a node that changes it repeatedly should not accumulate.
        if len(_cache) > 8:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:-4]:
                _cache.pop(k, None)
        return payload
