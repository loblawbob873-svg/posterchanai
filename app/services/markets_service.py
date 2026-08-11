"""Markets — a crypto price + news digest (Discover → Markets in the client).

Design (kept deliberately simple so it's stable): ONE shared report is produced by the SCHEDULER and cached
as an operator-signed Nostr doc (kind-30078, d=pcai:markets:daily) + held in-process. The request path
(get_report / GET /api/markets) is a PURE SERVER — it never generates and never reads the relay — so a
burst of viewers costs nothing and can't storm the AI backend. All generation is owned by the scheduler and
funnelled through ONE single-flight task handle:

  • cron 08:00 + 15:00 (server-local) → refresh the digest;
  • a light interval → populate a cold node / retry after a failed run (only fires work while there's no
    good report yet — a no-op once populated);
  • the first cold read kicks exactly one populate so a just-installed node isn't blank until the interval.

Because get_report never launches generation, there's no cooldown/last-attempt bookkeeping to get wrong.
The feature is disabled entirely on a Nostr-only install or a node with no chat servers (_ai_available)."""
import asyncio
import logging
import time

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.database import SessionLocal
from app.services import settings_store as _ss
from app.services import nostr_store as _store
from app.services.search_service import SearchService
from app.services.chat_service import ChatService

logger = logging.getLogger(__name__)

D_TAG = "pcai:markets:daily"

# The coins to digest. XRP / Bitcoin / Ethereum / Monero were requested; the rest are the most-followed
# large caps. Keep the list modest — each entry is 1-2 searches + 1 LLM summary, at most twice a day.
_COINS = [
    ("BTC", "Bitcoin"),
    ("ETH", "Ethereum"),
    ("XRP", "XRP"),
    ("XMR", "Monero"),
    ("SOL", "Solana"),
    ("DOGE", "Dogecoin"),
    ("ADA", "Cardano"),
    ("BNB", "BNB"),
]

# CoinGecko ids for the same list. Prices come from a PRICE API, never from the news text: asking the model
# to read a price out of search snippets meant it quoted whatever figure an old article happened to mention
# — XRP came out at $0.62 against a real $1.14, ADA at $0.92 against $0.173, and Monero/SOL/DOGE simply had
# no price at all because none of their articles printed one.
_CG_IDS = {
    "BTC": "bitcoin", "ETH": "ethereum", "XRP": "ripple", "XMR": "monero",
    "SOL": "solana", "DOGE": "dogecoin", "ADA": "cardano", "BNB": "binancecoin",
}
_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price"
_PRICE_TIMEOUT = 20.0


async def _fetch_prices() -> dict:
    """{SYM: {"usd": float, "chg24h": float}} for every coin, in ONE request. Never raises — on failure the
    digest just runs without authoritative prices (and says so) rather than inventing them."""
    import httpx
    ids = ",".join(_CG_IDS[s] for s, _ in _COINS if s in _CG_IDS)
    try:
        async with httpx.AsyncClient(timeout=_PRICE_TIMEOUT) as client:
            r = await client.get(_PRICE_URL, params={
                "ids": ids, "vs_currencies": "usd", "include_24hr_change": "true",
            })
        if r.status_code != 200:
            logger.warning("[markets] price fetch HTTP %s", r.status_code)
            return {}
        data = r.json() or {}
    except Exception as e:
        logger.warning("[markets] price fetch failed: %s", e)
        return {}
    out = {}
    for sym, _name in _COINS:
        row = data.get(_CG_IDS.get(sym, "")) or {}
        usd = row.get("usd")
        if isinstance(usd, (int, float)):
            out[sym] = {"usd": float(usd), "chg24h": float(row.get("usd_24h_change") or 0.0)}
    return out


# ---- live prices, for the desktop ticker widget -------------------------------------------------
# The digest above is generated twice a day; a ticker is useless at that rate. This is the same ONE
# upstream request, cached IN PROCESS and shared by every viewer — a desktop full of ticker widgets, on
# every open client, costs this node at most one CoinGecko call per _PRICES_TTL. Serving the stale copy
# while a refresh is in flight is deliberate: a rate-limited or slow upstream must degrade to "the price
# is a minute old", never to a widget that empties itself.
_PRICES_TTL = 90.0
_prices_cache: dict = {}
_prices_at: float = 0.0
_prices_lock: asyncio.Lock = None


async def get_prices() -> dict:
    """{"at": epoch, "prices": {SYM: {"usd", "chg24h"}}} — cached, never raises."""
    global _prices_cache, _prices_at, _prices_lock
    now = time.time()
    if _prices_cache and (now - _prices_at) < _PRICES_TTL:
        return {"at": int(_prices_at), "prices": _prices_cache}
    if _prices_lock is None:
        _prices_lock = asyncio.Lock()
    # Single-flight: a burst of cold clients must not each launch the same request.
    async with _prices_lock:
        now = time.time()
        if _prices_cache and (now - _prices_at) < _PRICES_TTL:
            return {"at": int(_prices_at), "prices": _prices_cache}
        fresh = await _fetch_prices()
        if fresh:
            _prices_cache, _prices_at = fresh, time.time()
        elif _prices_cache:
            # Upstream said no. Keep the old numbers AND the old timestamp, so the client can see for
            # itself how stale they are rather than being told a failure was a refresh.
            return {"at": int(_prices_at), "prices": _prices_cache, "stale": True}
    return {"at": int(_prices_at), "prices": _prices_cache}


def _fmt_price(usd: float) -> str:
    """Sub-dollar coins need real precision — DOGE at $0.07 and ADA at $0.17 both round to $0.00 at 2dp."""
    if usd >= 1:
        return f"${usd:,.2f}"
    return f"${usd:.6f}".rstrip("0").rstrip(".")

_COIN_TIMEOUT = 120.0          # hard cap per coin — a hung search/LLM yields an empty card, never stalls the run
_CONCURRENCY = 3               # coins in flight at once: parallelizes the SearXNG I/O (LLM calls still queue at
                               # the LB) → cuts wall time without a thundering herd on a small self-hosted box.
                               # Worst-case run ≈ ceil(8/3)=3 batches × _COIN_TIMEOUT = 360s (the client polls
                               # longer than this before giving up — keep them in sync).
_POPULATE_MIN = 5              # interval (minutes): retry populating a cold/failed node; no-op once populated

_scheduler = None
_genlock = asyncio.Lock()      # serialize the actual digest pass (belt-and-suspenders under the single-flight)
_gen_task = None               # THE single in-flight generation task — every launch path rides this one handle
_memo = None                   # last good report held in-process → served to everyone (cron refreshes it)
_degraded = False              # last completed attempt produced nothing usable AND there's no good report to
                               # fall back on → the client shows a clear "unavailable" instead of a forever spinner


def _op_sk():
    """Operator seckey from the keyfile — READ-ONLY (never mints). A public /api/markets read must not
    create an operator identity as a side effect; main.py resolves/mints the key at startup. None until
    it exists."""
    try:
        from app.services import keystore
        from app.services.nostr import nostr_service
        nsec = keystore.get_operator_nsec()
        return nostr_service.decode_seckey(nsec) if nsec else None
    except Exception:
        return None


def _port() -> int:
    return _ss.get_int("nostr_relay_port", 3052)


def _ai_available() -> bool:
    """Markets needs the AI backend (web search + LLM summary). Disabled on a Nostr-only install and on
    any node with no chat servers configured — so a stripped-down deployment doesn't run a pointless job
    or surface a broken feature."""
    import os
    if os.getenv("POSTERCHANAI_NOSTR_ONLY", "0").strip().lower() in ("1", "true", "yes", "on"):
        return False
    try:
        return bool((_ss.get("chat_server_urls", "") or "").strip())
    except Exception:
        return False


async def _gen_coin(sym: str, name: str, price: dict = None) -> dict:
    """One coin → {sym, name, summary, articles}. Web search (news-first, proxy handled by the search
    service) + a short AI briefing that leads with the price. Never raises — a failed coin yields a card
    with whatever it got (possibly empty). Opens its OWN short-lived DB session so a slow LLM/search call
    can't hold one session open across all ~8 coins (the long-held-connection-death hazard)."""
    db = SessionLocal()
    try:
        ss = SearchService(db)
        # The price no longer comes from here, so the query asks for NEWS rather than "price today …
        # coindesk". That brand-anchored phrasing skewed results toward whatever CoinDesk had covered,
        # which is why Monero — plenty of news, rarely on CoinDesk's front page — kept coming back thin.
        query = f"{name} ({sym}) cryptocurrency latest news"
        # Two independent searches: the news-filtered one, and (only if it yields nothing OR errors) a
        # plain general search. Separate try/except so an EXCEPTION in the first still tries the fallback.
        results = []
        try:
            results = await ss.web_search(query, limit=6, categories="news", time_range="week", sort_recent=True)
        except Exception as e:
            logger.debug("[markets] news search %s failed: %s", sym, e)
        if not results:
            try:
                results = await ss.web_search(query, limit=6)
            except Exception as e:
                logger.debug("[markets] fallback search %s failed: %s", sym, e)

        articles = []
        for r in (results or [])[:6]:
            u = (r.get("url") or "").strip()
            if u:
                articles.append({"title": (r.get("title") or u), "url": u, "published": (r.get("published") or "")})

        # Live price line, handed to the model as the ONLY acceptable price. Without this it quoted stale
        # figures out of the articles; with it, the briefing and the card header always agree.
        price_line = ""
        if price and isinstance(price.get("usd"), float):
            _chg = price.get("chg24h") or 0.0
            price_line = f"{_fmt_price(price['usd'])} ({_chg:+.2f}% over 24h)"

        summary = ""
        if results:
            ctx = ""
            if price_line:
                ctx += f"CURRENT PRICE of {name} ({sym}), live from a price API: {price_line}\n\n"
            ctx += f"Search results for {name} ({sym}) cryptocurrency — latest news:\n\n"
            for i, r in enumerate(results, 1):
                _p = f" (published {r['published']})" if r.get("published") else ""
                ctx += f"{i}. {r.get('title', '')}{_p}\n{r.get('url', '')}\n{r.get('content', '')}\n\n"
            sysmsg = (
                f"You are a concise crypto market analyst. Write a SHORT briefing (3-4 sentences) on "
                f"{name} ({sym}). "
                + (f"LEAD with this exact price and 24h move: {price_line}. That figure is authoritative — "
                   f"any price mentioned in the search results is older and MUST be ignored, never repeated. "
                   if price_line else
                   "No live price was available, so say so in the first sentence and do not quote a price "
                   "from the articles — they may be months out of date. ")
                + f"Then give the most important recent development from the results. Use ONLY facts present "
                  f"above. No preamble, no markdown headers."
            )
            try:
                chat = ChatService(db, user=None)
                summary = (await chat.chat([
                    {"role": "system", "content": sysmsg},
                    {"role": "user", "content": ctx},
                ]) or "").strip()
                # chat() NEVER raises — it returns an "Error: ..." string when the LLM/LB is down. Don't
                # store that as the briefing (it would be served until the next refresh).
                if summary.startswith("Error:"):
                    summary = ""
            except Exception as e:
                logger.debug("[markets] summarize %s failed: %s", sym, e)
                summary = ""

        # price/chg24h ride along as real fields so the CARD renders them directly — the number a reader
        # sees never depends on the model having repeated it correctly (or at all).
        out = {"sym": sym, "name": name, "summary": summary, "articles": articles}
        if price and isinstance(price.get("usd"), float):
            out["price"] = price["usd"]
            out["price_str"] = _fmt_price(price["usd"])
            out["chg24h"] = price.get("chg24h") or 0.0
        return out
    finally:
        db.close()


async def _gen_coin_guarded(sym: str, name: str, sem: asyncio.Semaphore, price: dict = None) -> dict:
    """One coin, bounded by the concurrency semaphore and a hard per-coin timeout so a single hung
    search/LLM can't stall the whole digest. Never raises → asyncio.gather never raises."""
    async with sem:
        try:
            return await asyncio.wait_for(_gen_coin(sym, name, price), timeout=_COIN_TIMEOUT)
        except Exception as e:
            logger.debug("[markets] coin %s timed out / failed: %s", sym, e)
            # Keep the price even when the search/LLM half timed out — a card showing the live price with
            # no briefing is still useful, and it's the part that was wrong before.
            out = {"sym": sym, "name": name, "summary": "", "articles": []}
            if price and isinstance(price.get("usd"), float):
                out["price"], out["price_str"] = price["usd"], _fmt_price(price["usd"])
                out["chg24h"] = price.get("chg24h") or 0.0
            return out


async def generate_report() -> dict:
    """Run the full digest, update the in-process memo, and publish it as the operator's kind-30078 doc.
    A run that produced NOTHING usable (search + LLM both down) does NOT overwrite a prior good report —
    it keeps serving the last good one rather than clobbering it with an empty digest."""
    global _memo, _degraded
    async with _genlock:                       # serialize: never two digest passes at once
        prices = await _fetch_prices()         # ONE request for all 8, before any per-coin work
        sem = asyncio.Semaphore(_CONCURRENCY)
        coins = list(await asyncio.gather(*[
            _gen_coin_guarded(s, n, sem, prices.get(s)) for s, n in _COINS
        ]))
    # Merge: for any coin THIS run couldn't fetch, keep the previous report's card. So a flaky/partial run
    # (e.g. 2 of 8 coins returned) never blanks a coin or clobbers a complete digest with mostly-empty cards.
    if _memo and _memo.get("coins"):
        prev = {c.get("sym"): c for c in _memo["coins"]}
        for c in coins:
            if not (c.get("summary") or c.get("articles")):
                p = prev.get(c.get("sym"))
                if p and (p.get("summary") or p.get("articles")):
                    c["summary"], c["articles"] = p.get("summary", ""), p.get("articles", [])
    report = {"generated_at": time.time(), "coins": coins}
    # A live price counts as usable content: if search/LLM are down but the price API answered, a digest of
    # correct prices is worth serving — discarding it would have been throwing away the reliable half.
    if not any(c.get("summary") or c.get("articles") or c.get("price") for c in coins):
        logger.warning("[markets] generation produced no usable content; keeping previous report")
        if _memo is None:
            _degraded = True                   # nothing good to show and the run failed → 'unavailable'
        return _memo or report
    _memo = report
    _degraded = False                          # recovered
    sk = _op_sk()
    if sk:
        try:
            await _store.put_doc(_port(), sk, D_TAG, report)
        except Exception as e:
            logger.warning("[markets] publish failed: %s", e)
    return report


async def _read_cached() -> dict | None:
    sk = _op_sk()
    if not sk:
        return None
    try:
        doc = await _store.get_doc(_port(), D_TAG, seckey=sk)
        return doc if isinstance(doc, dict) and doc.get("coins") else None
    except Exception:
        return None


async def _populate() -> None:
    """Bring a cold node up to date WITHOUT a redundant regeneration: adopt the report this node's last cron
    already published to the local relay if there is one (survives a restart); only generate from scratch
    otherwise."""
    global _memo
    if _memo is not None:
        return
    cached = await _read_cached()
    if cached:
        _memo = cached
        return
    await generate_report()


def _launch(coro_fn) -> None:
    """Single-flight launcher — ALL generation (cron refresh, cold populate, interval retry, first read)
    goes through here, so at most one runs at a time and get_report's in-flight check always sees it.
    Sync check+assign (no await between) → no TOCTOU. A done-callback surfaces a crash that would
    otherwise be a silent 'Task exception was never retrieved'."""
    global _gen_task
    if _gen_task is not None and not _gen_task.done():
        return
    _gen_task = asyncio.create_task(coro_fn())

    def _cb(t):
        try:
            t.result()
        except Exception as e:   # pragma: no cover
            logger.warning("[markets] background generation crashed: %s", e)
    _gen_task.add_done_callback(_cb)


async def get_report() -> dict:
    """PURE SERVER — no generation, no relay read on the request path. Serves the in-process report; if
    there isn't one yet it kicks exactly one populate (only when nothing has ever been launched) and tells
    the client whether it's building or genuinely unavailable. The scheduler owns all refresh/retry."""
    if not _ai_available():
        return {"generated_at": 0, "coins": [], "disabled": True}
    if _memo is not None:
        return _memo
    if _gen_task is None:                # first-ever cold read → one populate; retries thereafter are the interval's job
        _launch(_populate)
    if _gen_task is not None and _gen_task.done() and _degraded:
        return {"generated_at": 0, "coins": [], "unavailable": True}
    return {"generated_at": 0, "coins": [], "generating": True}


# ---- scheduler ----
async def _cron_job():
    if not _ai_available():
        return              # nostr-only / no AI backend → nothing to generate
    _launch(generate_report)   # force a refresh at 08:00 / 15:00 (rides the single-flight)


async def _interval_job():
    if not _ai_available() or _memo is not None:
        return              # already populated → cheap no-op; else populate / retry a failed cold start
    _launch(_populate)


def start_markets_scheduler():
    global _scheduler
    if _scheduler:
        return
    _scheduler = AsyncIOScheduler()
    # cron (not interval): a wall-clock trigger does NOT fire on registration, so a restart won't cause an
    # extra run. Twice daily at 08:00 and 15:00 SERVER-LOCAL time (AsyncIOScheduler's default tz).
    _scheduler.add_job(_cron_job, CronTrigger(hour="8,15", minute=0), id="markets_cron",
                       name="Markets digest refresh", replace_existing=True, max_instances=1,
                       coalesce=True, misfire_grace_time=3600)
    # light retry/populate loop — only does work while there's no good report yet (no-op once populated)
    _scheduler.add_job(_interval_job, IntervalTrigger(minutes=_POPULATE_MIN), id="markets_populate",
                       name="Markets populate/retry", replace_existing=True, max_instances=1, coalesce=True)
    _scheduler.start()
    logger.info("[markets] scheduler started (refresh 08:00 + 15:00 local; populate every %dm)", _POPULATE_MIN)
    # Warm _memo now (adopt the report the last cron published to the relay) so a read right after a restart
    # serves it immediately instead of a 'building' spinner. Skipped/no-op if AI is off or already populated.
    if _ai_available():
        _launch(_populate)


def stop_markets_scheduler():
    global _scheduler
    if _scheduler:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
