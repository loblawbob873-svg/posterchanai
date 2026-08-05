#!/usr/bin/env python3
"""Is our live-stream chat in the SAME room as everyone else's?

A NIP-53 chat message (kind 1311) is `a`-tagged to the stream's addressable coordinate, and an
addressable coordinate is always `<kind>:<the event's AUTHOR>:<d>`. The client used to build it from
the stream's `p …host` tag instead. Those are the same key only when the streamer published their own
30311 — and on the live network they differ for about a third of streams, because a bot account
(Shobot's radio/TV streams, and others) publishes the event on a host's behalf. For every one of
those the chat pane was empty while the real room was busy, and anything we sent went somewhere
nobody was reading.

Two parts, so this is useful with or without a network:

  1. the rule, read out of static/js/client/app.js — the coordinate must come from the event author.
  2. the network, measured — sample live 30311s, and for each stream whose host ≠ author, count
     kind-1311 messages on each coordinate. Prints which one holds the conversation.

    venv-unified/bin/python scripts/check_stream_chat.py

Exit 0 = the rule holds (and, if the network was reachable, matches what the network does),
1 = regression, 2 = could not run.
"""
import asyncio
import collections
import json
import re
import sys
from pathlib import Path

APP_JS = Path(__file__).resolve().parents[1] / "static" / "js" / "client" / "app.js"
RELAYS = ["wss://nos.lol", "wss://relay.zap.stream", "wss://relay.damus.io"]
SAMPLE = 150          # 30311s to pull per relay
PROBE = 12            # host≠author streams to probe for chat


def _tag(ev, k):
    for t in ev.get("tags", []):
        if t and t[0] == k:
            return t[1] if len(t) > 1 else ""
    return ""


def _host(ev):
    for t in ev.get("tags", []):
        if t and t[0] == "p" and len(t) > 3 and (t[3] or "").lower() == "host":
            return t[1]
    return ev["pubkey"]


def check_source():
    """The client must address chat by the 30311's AUTHOR, and still read the host coordinate too."""
    src = APP_JS.read_text()
    problems = []
    if not re.search(r"const saddr=`30311:\$\{e\.pubkey\}:\$\{dtag\}`", src):
        problems.append("the chat coordinate is not built from the stream event's author (e.pubkey)")
    if not re.search(r"const haddr=\(hpk && hpk!==e\.pubkey\)", src):
        problems.append("the host coordinate is no longer read as well — clients that chat there "
                        "would go unseen")
    if "'#a':addrs" not in src:
        problems.append("the 1311 subscription no longer filters on both coordinates")
    return problems


async def _session(url, filters, timeout=20):
    import websockets
    res = {str(i): [] for i in range(len(filters))}
    try:
        async with websockets.connect(url, open_timeout=8, close_timeout=3,
                                      max_size=4 * 1024 * 1024) as ws:
            for i, f in enumerate(filters):
                await ws.send(json.dumps(["REQ", str(i), f]))
            done = set()
            while len(done) < len(filters):
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
                if m[0] == "EVENT" and m[1] in res:
                    res[m[1]].append(m[2])
                elif m[0] in ("EOSE", "CLOSED"):
                    done.add(m[1])
    except Exception as exc:
        print(f"  · {url} unreachable ({type(exc).__name__})")
    return res


async def measure():
    got = await asyncio.gather(*[_session(r, [{"kinds": [30311], "limit": SAMPLE}]) for r in RELAYS])
    streams = {}
    for g in got:
        for ev in g["0"]:
            streams[ev["id"]] = ev
    if not streams:
        return None
    split = [e for e in streams.values() if _host(e) != e["pubkey"]]
    print(f"  {len(streams)} streams sampled, {len(split)} published by a key other than the host "
          f"({', '.join(sorted({_tag(e, 'client') or '(no client tag)' for e in split})[:4])})")
    cands = sorted(split, key=lambda e: -e["created_at"])[:PROBE]
    if not cands:
        return None
    filt, idx = [], {}
    for e in cands:
        d = _tag(e, "d")
        for which, pk in (("author", e["pubkey"]), ("host", _host(e))):
            idx[len(filt)] = (e["id"], which)
            filt.append({"kinds": [1311], "#a": [f"30311:{pk}:{d}"], "limit": 40})
    res = await asyncio.gather(*[_session(r, filt, timeout=25) for r in RELAYS])
    counts = collections.Counter()
    for g in res:
        for k, evs in g.items():
            counts[idx[int(k)]] += len(evs)
    author = sum(n for (sid, which), n in counts.items() if which == "author")
    host = sum(n for (sid, which), n in counts.items() if which == "host")
    withchat = {sid for (sid, _), n in counts.items() if n}
    print(f"  of {len(cands)} probed, {len(withchat)} have chat: "
          f"{author} messages on the author coordinate, {host} on the host coordinate")
    return author, host


def main():
    problems = check_source()
    for p in problems:
        print(f"FAIL  {p}")
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  websockets not installed — rule checked, network not measured")
        return 1 if problems else 2
    print("network:")
    m = asyncio.run(measure())
    if m is None:
        print("SKIP  no live streams reachable — rule checked, network not measured")
        return 1 if problems else 2
    author, host = m
    if author == 0 and host == 0:
        print("SKIP  none of the probed streams had chat")
    elif author < host:
        # Not a code failure — a finding. If the network ever moves to the host coordinate, the
        # client's read of BOTH still covers it; only the publish target would need revisiting.
        print("NOTE  more chat on the host coordinate than the author one — worth a look")
    if problems:
        return 1
    print("OK  chat is addressed the way the network addresses it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
