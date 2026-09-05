#!/usr/bin/env python3
"""Web Search measured as a RATE, and the engine-proxy config that decides it.

    venv-unified/bin/python scripts/check_websearch_rate.py [trials]

WHY A RATE. Reported as "Web Search has been acting weird all day, usually does not work the first
time — this test failed like 3 times before loading results". Every single-shot check of this feature
passed, because a search that works one time in three works when you look at it. It has to become a
number before anything can be called fixed: measured on server1 before the fix, **3 of 10** searches
returned results, and the seven failures were HTTP **200** with an empty result list after exactly
**12.0s** — the engine timeout the node's own settings.yml sets, hit by every engine at once.

The cause was not the query and not the app: `searxng_proxy_engines` shipped ON, so the bundled
instance made every engine request through the node's Tor fallback listener. A Tor exit is a shared
address — Brave and Google CSE answered "too many requests", DuckDuckGo and Wikidata "access denied",
and SearXNG suspends an engine that replies that way for an HOUR. What survived rode 6.5-12.0s
round-trips against a 12.0s ceiling, so whether a search returned anything was a coin flip on circuit
latency. The same queries direct: 0.5-1.6s, results every time.

TWO CHECKS, because either one alone would have missed it.

  rate    Run `trials` DISTINCT real queries through this node's own `search_page` — the exact code
          path Discover → Web Search uses, resolver included — and require most of them to answer.
          Distinct queries on purpose: repeating one measures SearXNG's result cache, which is warm
          after the first hit and would report a healthy rate over a node that cannot reach an engine.

  speed   How long those searches took, which is the sensitive half and the reason the rate alone is
          not enough: re-measured on the broken node with well-known queries, 8 of 8 still returned
          something — Google answers a popular query even over Tor — while most took 8-12s against
          the 12s engine ceiling. A search that ends on that ceiling did not finish; it returned
          whatever had arrived, and on a query the engines have not cached that is nothing. So the
          rate looked perfect on the very configuration that gave the owner three empty searches in a
          row. Measured both ways on server1, same minutes, same query pool: direct 0.3-3.8s and
          10/10 answered, through the proxy 6.5-12.0s and 1/10.

          An ABSOLUTE budget, deliberately, and not "within a slack of `outgoing.request_timeout`" —
          which is what this measured first and got wrong. The timeout is read from settings.yml, but
          the RUNNING instance loaded that file when IT started, so a file edited since describes a
          process that is not using it: on the node this was written on, a settings.yml already
          rewritten to go direct made the check compare 12s searches against the 3.0s default and
          call all 8 cut off. A number the instance cannot contradict is worth more here than a
          precise one derived from the wrong place, and SearXNG's /config does not publish the real
          one.

  config  A pure-file assertion that needs no network and cannot be flaky: settings.yml must not
          carry a managed `outgoing.proxies` block while `searxng_proxy_engines` is off. That is the
          half a rate check cannot see on a good Tor day, and it is also the half that was UNFIXABLE
          from the admin panel — `posterchanai-searxng.service` runs a bare `python -m
          app.services.searxng_native`, whose settings cache is EMPTY, so the toggle read as its own
          default and the block was rewritten at every start no matter what the operator chose.
          An `outgoing:` block written by hand OUTSIDE our markers is the operator's and is ignored
          here, exactly as `apply_outgoing_proxy` ignores it.

Exit 0 = the rate held and the config agrees with the setting, 1 = either failed, 2 = could not run
(no SearXNG installed, search switched off for this node, no database).
"""
import asyncio
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 8
# Ordinary queries that unambiguously have results on any working engine. If one of these comes back
# empty it is the search that is broken, not the query — which is the whole point: a nonsense string
# returns zero results honestly, and using one would make this check unable to tell the two apart.
#
# SAMPLED AT RANDOM FROM A POOL MUCH LARGER THAN `trials`, AND THAT IS NOT DECORATION. A fixed list
# measures the engines' cache from the second run onward: the first run of this check on the broken
# node reported 8.4-12.0s, and an immediate re-run of the SAME eight queries reported 0.9-2.3s and a
# clean pass, on a configuration that was still cutting off most searches. Whatever the pool, a run
# must ask things the last run did not.
QUERY_POOL = [
    "python asyncio tutorial", "postgres index bloat", "ffmpeg concat filter",
    "rust borrow checker", "nginx reverse proxy websocket", "sqlite wal mode",
    "systemd service restart policy", "linux oom killer tuning", "curl follow redirects",
    "git rebase interactive", "docker compose healthcheck", "openssl self signed certificate",
    "how to pickle a numpy array", "reverse a linked list in c", "what causes high load average",
    "difference between tcp and udp sockets", "read a core dump with gdb", "why is my docker build slow",
    "btrfs subvolume snapshots", "how does dns caching work", "memory barrier in c++",
    "how to profile a python script", "bash trap on exit", "awk print second column",
    "rsync exclude directory", "tar extract single file", "ssh port forwarding example",
    "iptables list rules", "lvm extend logical volume", "zfs scrub schedule",
    "kernel module signing", "cron every five minutes", "jq filter nested array",
    "sed replace in place", "find files larger than", "du sort by size",
]
# Below this, search is broken however good the excuse. Not 100%: one engine having a bad minute is
# ordinary and must not red the suite, while the 30% this was built to catch has to.
MIN_RATE = 0.80
# Longer than this and the engine hop is not going straight out. A bundled SearXNG talking to engines
# directly answers in about a second — measured here 0.3-3.8s over ten queries — while the same
# instance proxied through Tor took 6.5-12.0s and spent most searches on its own 12s ceiling. 5s sits
# clear of both, so this does not depend on how the timeout happens to be configured.
SLOW_SECS = 5.0
# How many searches may be that slow before the transport, not the query, is the problem. A third:
# one engine having a bad minute is normal; most searches crawling means every result set is
# truncated and the empty ones are luck.
MAX_SLOW = 0.34
# SearXNG's own default when nothing overrides it (searx/settings_defaults.py). Reported for context
# only — never used as a threshold, see the docstring.
_DEFAULT_ENGINE_TIMEOUT = 3.0


def skip(msg):
    print(f"SKIP: {msg}")
    sys.exit(2)


def hydrate():
    """Load this node's real settings, the way the app does at startup.

    Without this the check runs with an EMPTY settings cache and every read answers with its own
    default — which is precisely the bug in `_proxy_wanted` that let the admin toggle do nothing, and
    a check that reproduced it would compare the file against a default instead of against what the
    operator chose. Best-effort: a node with no database still gets the rate half.
    """
    try:
        from app.database import SessionLocal
        from app.services import settings_store
        settings_store.load_local()
        db = SessionLocal()
        try:
            settings_store.hydrate_from_db(db)
        finally:
            db.close()
    except Exception as exc:
        print(f"  (settings not hydrated: {exc})")


def check_config() -> list:
    """Does settings.yml agree with the setting? Returns a list of failure strings."""
    from app.services import searxng_native, settings_store
    path = searxng_native.settings_path()
    if not path.is_file():
        return []                      # nothing bundled here; the rate half will skip too
    text = path.read_text(encoding="utf-8")
    begin = searxng_native._PROXY_BEGIN
    if begin not in text:
        return []                      # operator-managed or never seeded — not ours to judge
    managed = text.split(begin, 1)[1].split(searxng_native._PROXY_END, 1)[0]
    proxied = bool(re.search(r"^\s*proxies:", managed, re.M))

    # The setting can only be read where the cache is hydrated. Where it is NOT — which is the unit's
    # own situation — the honest answer is that this check cannot compare, not that the file is wrong.
    if not settings_store.is_hydrated():
        print(f"  config: settings not hydrated here; settings.yml has proxies={proxied} (not compared)")
        return []
    wanted = settings_store.get_bool("searxng_proxy_engines", False)
    print(f"  config: searxng_proxy_engines={wanted}  settings.yml proxies={proxied}  ({path})")
    if proxied and not wanted:
        return ["settings.yml routes engine requests through a proxy while searxng_proxy_engines "
                "is OFF — engines will suspend the shared exit and searches come back empty"]
    if wanted and not proxied:
        return ["searxng_proxy_engines is ON but settings.yml has no proxy block — the toggle did "
                "not reach the file (is the process that wrote it able to read settings?)"]
    return []


def engine_timeout() -> float:
    """`outgoing.request_timeout` from settings.yml, else SearXNG's default. CONTEXT FOR THE LOG ONLY.

    Nothing is asserted against this: the file can have been rewritten since the instance read it,
    which is exactly the state this check found the node in.
    """
    try:
        from app.services import searxng_native
        text = searxng_native.settings_path().read_text(encoding="utf-8")
        m = re.search(r"^\s*request_timeout:\s*([0-9.]+)", text, re.M)
        if m:
            return float(m.group(1))
    except Exception:
        pass
    return _DEFAULT_ENGINE_TIMEOUT


async def run_rate() -> list:
    from app.database import SessionLocal
    from app.services.search_service import get_search_service, resolve_searxng_url

    base = resolve_searxng_url()
    if not base:
        skip("web search is switched off for this node (searxng_enabled)")
    print(f"  searching via {base}  "
          f"(settings.yml says engine timeout {engine_timeout():.1f}s; "
          f"slow means over {SLOW_SECS:.0f}s)")

    db = SessionLocal()
    try:
        svc = get_search_service(db)
        ok = 0
        slow = 0
        fails = []
        queries = random.sample(QUERY_POOL, min(TRIALS, len(QUERY_POOL)))
        while len(queries) < TRIALS:                   # more trials than pool: reuse, still shuffled
            queries += random.sample(QUERY_POOL, min(TRIALS - len(queries), len(QUERY_POOL)))
        for i in range(TRIALS):
            q = queries[i]
            t0 = time.time()
            try:
                r = await svc.search_page(q, category="general", page=1, limit=20)
            except Exception as exc:                      # search_page is documented never to raise
                fails.append(f"#{i+1} {q!r}: raised {exc!r}")
                print(f"  {i+1:2}/{TRIALS} {q!r}: RAISED {exc!r}")
                continue
            dt = time.time() - t0
            n = len(r.get("results") or [])
            err = r.get("error") or ""
            is_slow = dt >= SLOW_SECS
            if is_slow:
                slow += 1
            mark = "  <- SLOW: the engine hop is not going straight out" if is_slow else ""
            if n:
                ok += 1
                print(f"  {i+1:2}/{TRIALS} {q!r}: {n} results in {dt:.1f}s{mark}")
            else:
                fails.append(f"#{i+1} {q!r}: 0 results in {dt:.1f}s ({err or 'no error reported'})")
                print(f"  {i+1:2}/{TRIALS} {q!r}: 0 RESULTS in {dt:.1f}s  "
                      f"{err or '(reported no error)'}{mark}")
    finally:
        db.close()

    problems = []
    rate = ok / float(TRIALS)
    print(f"\n  rate: {ok}/{TRIALS} searches returned results ({rate*100:.0f}%)")
    print(f"  speed: {slow}/{TRIALS} took over {SLOW_SECS:.0f}s")
    if rate < MIN_RATE:
        problems.append(f"only {ok}/{TRIALS} searches returned results "
                        f"({rate*100:.0f}%, need {MIN_RATE*100:.0f}%)")
        problems += fails
    if slow / float(TRIALS) > MAX_SLOW:
        # Deliberately a failure even when the rate held: a truncated result set is what an empty one
        # is made of, and the node this was written on passed the rate at 8/8 while most of those
        # searches were riding the engine timeout. Reporting only the rate is how this stayed open.
        problems.append(
            f"{slow}/{TRIALS} searches took over {SLOW_SECS:.0f}s — a bundled SearXNG answers in "
            f"about a second, so the engine hop is being proxied or is failing over. Results that "
            f"do arrive are truncated at the engine timeout and an uncached query returns nothing. "
            f"Check Admin → Tools, searxng_proxy_engines.")
    return problems


def main():
    try:
        from app.services import searxng_native
    except Exception as exc:
        skip(f"cannot import the app ({exc})")
    if not searxng_native.available():
        skip("no bundled SearXNG on this node (./install.sh --searxng)")

    print("Web Search rate + engine-proxy config")
    hydrate()
    problems = []
    try:
        problems += check_config()
    except Exception as exc:
        skip(f"could not read the SearXNG settings ({exc})")
    try:
        problems += asyncio.run(run_rate())
    except SystemExit:
        raise
    except Exception as exc:
        skip(f"could not run searches ({exc})")

    if problems:
        print("\nFAIL:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\nPASS: search answers reliably and settings.yml agrees with the toggle")
    return 0


if __name__ == "__main__":
    sys.exit(main())
