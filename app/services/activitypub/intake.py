"""WHAT EACH FEDIVERSE SERVER SENT US, AND WHAT BECAME OF IT -- logged every 10 minutes.

2026-10-17: posts from nicecrew.digital stopped arriving (about 100 an hour down to 15) while the inbox went on
answering ~1,000 deliveries an hour with 202, and nothing in any log could say whether nicecrew had stopped sending,
or sent and was refused, or was accepted and then ignored: the route logged only its status code (and the access
log only ever shows the front proxy's address), and `inbox.process` RETURNED its verdict ("stored", "ignored:
nobody here follows ...") to a caller that threw it away.

One line per window, busiest senders first:
  [activitypub] intake 600s: nicecrew.digital 52 (scheduled 52 -> stored 30, already stored 10, ignored: ... 12); ...
Counts only -- never a post's text, an account or an object id.
"""
from __future__ import annotations

import logging
import time
from collections import Counter

logger = logging.getLogger(__name__)

EVERY = 600.0
TOP = 12

_received: dict = {}          # host -> Counter of what the route did with it
_processed: dict = {}         # host -> Counter of what processing made of it
_since = time.monotonic()


def _host(host: str) -> str:
    return (host or "?").lower()[:80]


def received(host: str, what: str) -> None:
    """The route's answer: scheduled / relay / blocked / own / 401 ... / 429 / 503 ..."""
    _received.setdefault(_host(host), Counter())[what] += 1
    report()


def processed(host: str, what: str) -> None:
    """What processing made of a scheduled activity -- `inbox.process`'s verdict, or the error class."""
    _processed.setdefault(_host(host), Counter())[_short(what)] += 1


def _short(what: str) -> str:
    what = str(what or "?")
    # "relay refused: <message>" carries the relay's own words; keep the class, not an id it may quote.
    for head in ("relay refused", "error", "failed"):
        if what.startswith(head):
            return what.split(":", 1)[0] + (":" + what.split(":", 2)[1] if what.count(":") >= 2 else "")
    return what[:80]


def lines(now: float | None = None) -> list:
    """The current window, busiest sender first (for the log line and for tests)."""
    hosts = set(_received) | set(_processed)
    rows = sorted(hosts, key=lambda h: -sum((_received.get(h) or Counter()).values()))
    out = []
    for h in rows[:TOP]:
        r = _received.get(h) or Counter()
        p = _processed.get(h) or Counter()
        got = ", ".join(f"{k} {v}" for k, v in r.most_common())
        did = ", ".join(f"{k} {v}" for k, v in p.most_common())
        out.append(f"{h} {sum(r.values())} ({got}{' -> ' + did if did else ''})")
    return out


def report(now: float | None = None) -> bool:
    """Log and start a new window once EVERY seconds have passed. True when it logged."""
    global _since
    now = time.monotonic() if now is None else now
    if now - _since < EVERY:
        return False
    rows = lines()
    if rows:
        more = len(set(_received) | set(_processed)) - len(rows)
        logger.info("[activitypub] intake %ds: %s%s", int(now - _since), "; ".join(rows),
                    f"; +{more} more servers" if more > 0 else "")
    _received.clear()
    _processed.clear()
    _since = now
    return True
