#!/usr/bin/env python3
"""How many clients does ONE relay process serve, and where does its CPU go? Measured, not argued.

    # 1. a relay on a COPY of a store (never the production directory), on a spare port:
    venv-unified/bin/python scripts/relay_loadtest.py serve --data /usb/pcdb-scale/loadtest --port 13552
    # 2. clients that behave like the web client, ramped; prints relay CPU, p50/p99 answer latency, frames/s:
    venv-unified/bin/python scripts/relay_loadtest.py load --port 13552 --steps 50,150,400 --seconds 60

Each simulated client: connects with permessage-deflate (as browsers do), opens the Nostrverse timeline
(120 posts) and keeps it live, opens a profile (50 posts) and its kind-0, re-opens a timeline every 20 s
(a view switch), and publishes a signed post every 30 s with a throwaway key. Built for #19 (relay perf),
2026-10-17: "if our relay is performing this bad with barely any data/users, we can never scale."

The serving relay is the shipped RelayServer + PosterChanDB store with this node's own config, except: the
data directory and port given here, WoT off (open publishing, so throwaway keys are accepted), no upstreams,
no outbox, no Concord mirror -- none of which a client's read path touches.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
CPU_FILE = "/tmp/relay_loadtest.cpu"     # the server writes its own cumulative CPU seconds here


def _cpu_seconds() -> float:
    t = os.times()
    return t.user + t.system


async def serve(data: str, port: int) -> None:
    if os.path.realpath(data).startswith("/var/lib/posterchandb"):
        raise SystemExit("refusing the production store -- give a COPY")
    os.environ["POSTERCHANDB_DIR"] = data
    from websockets.asyncio.server import serve as ws_serve
    from app.services.nostr_relay import thread as T
    from app.services.nostr_relay.server import RelayServer
    from app.services.nostr_relay.wot import WotGate
    cfg = T._read_config()
    cfg.update(port=port, bind="127.0.0.1", wot_enabled=False, upstream=[], private_relays=[])
    loop = asyncio.get_running_loop()
    store = T._open_store(cfg, loop)
    gate = WotGate()
    gate.set_operator(cfg["operator"])
    gate.set_blocked(cfg["blocked_pubkeys"])
    server = RelayServer(store, gate, cfg)
    async def report():
        while True:
            with open(CPU_FILE, "w") as f:
                f.write("%f %f\n" % (time.monotonic(), _cpu_seconds()))
            await asyncio.sleep(1)
    asyncio.create_task(report())
    async with ws_serve(server.handle, "127.0.0.1", port, process_request=server.process_request,
                        max_size=cfg["max_message_size"], ping_interval=RelayServer.KEEPALIVE_S,
                        ping_timeout=120, max_queue=64, extensions=[T._relay_deflate()]):
        print("serving ws://127.0.0.1:%d on %s" % (port, data), flush=True)
        await asyncio.Future()


# ------------------------------------------------------------------------------------------------ the clients
class Stats:
    def __init__(self):
        self.lat, self.frames, self.errors, self.reqs = [], 0, 0, 0
        self.by = {}                                  # request type -> [latency ms]


async def _req(ws, filters, st: Stats, live=False, what="?"):
    sid = "lt%08x" % random.getrandbits(32)
    t0 = time.monotonic()
    await ws.send(json.dumps(["REQ", sid, *filters]))
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), 30))
        st.frames += 1
        if m[0] == "EOSE" and m[1] == sid:
            break
        if m[0] == "CLOSED" and m[1] == sid:
            st.errors += 1
            break
    st.lat.append((time.monotonic() - t0) * 1000)
    st.by.setdefault(what, []).append(st.lat[-1])
    st.reqs += 1
    if not live:
        await ws.send(json.dumps(["CLOSE", sid]))
    return sid


async def client(port: int, authors: list, st: Stats, stop: asyncio.Event):
    import websockets
    from websockets.extensions.permessage_deflate import ClientPerMessageDeflateFactory
    from app.services.nostr.event import build_event
    sk = os.urandom(32)
    feed = [{"kinds": [1, 6, 1068, 5, 30023], "limit": 120}]
    try:
        async with websockets.connect("ws://127.0.0.1:%d/relay" % port, max_size=None, open_timeout=30,
                                      extensions=[ClientPerMessageDeflateFactory()],
                                      additional_headers={"Origin": "http://127.0.0.1"}) as ws:
            async def drain():                      # live events keep arriving on the timeline subscription
                pass
            await _req(ws, feed, st, live=True, what="timeline-open")
            if authors:
                a = random.choice(authors)
                await _req(ws, [{"authors": [a], "kinds": [1, 6, 1111, 1068], "limit": 50},
                                {"authors": [a], "kinds": [0], "limit": 1}], st, what="profile")
            next_feed, next_post = time.monotonic() + random.uniform(5, 20), time.monotonic() + random.uniform(5, 30)
            while not stop.is_set():
                now = time.monotonic()
                if now >= next_feed:
                    await _req(ws, feed, st, what="timeline-switch")
                    next_feed = now + 20
                elif now >= next_post:
                    ev = build_event(sk, 1, "load test %d" % random.getrandbits(32), [])
                    await ws.send(json.dumps(["EVENT", ev]))
                    next_post = now + 30
                else:
                    try:
                        await asyncio.wait_for(ws.recv(), 0.5)
                        st.frames += 1
                    except asyncio.TimeoutError:
                        pass
    except Exception:
        st.errors += 1


def _relay_cpu():
    try:
        t, c = open(CPU_FILE).read().split()
        return float(t), float(c)
    except Exception:
        return None


async def load(port: int, steps: list, seconds: float) -> None:
    import websockets
    # a few real authors from the store, so profile reads hit real data
    async with websockets.connect("ws://127.0.0.1:%d/relay" % port, max_size=None) as ws:
        await ws.send(json.dumps(["REQ", "a", {"kinds": [1], "limit": 400}]))
        authors = set()
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), 30))
            if m[0] == "EOSE":
                break
            if m[0] == "EVENT":
                authors.add(m[2]["pubkey"])
    authors = sorted(authors)
    print("clients  relay-CPU  p50ms  p99ms  reqs/s  frames/s  errors")
    for n in steps:
        st, stop = Stats(), asyncio.Event()
        tasks = [asyncio.create_task(client(port, authors, st, stop)) for _ in range(n)]
        await asyncio.sleep(min(10, seconds / 4))          # let them connect and do their opening reads
        st.lat.clear(); st.frames = st.reqs = 0          # opening reads stay in st.by
        c0 = _relay_cpu()
        await asyncio.sleep(seconds)
        c1 = _relay_cpu()
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        cpu = (100 * (c1[1] - c0[1]) / (c1[0] - c0[0])) if c0 and c1 else float("nan")
        lat = sorted(st.lat) or [0]
        print("%7d  %8.0f%%  %5.0f  %5.0f  %6.1f  %8.0f  %6d" % (
            n, cpu, statistics.median(lat), lat[int(len(lat) * 0.99) - 1 if len(lat) > 1 else 0],
            st.reqs / seconds, st.frames / seconds, st.errors), flush=True)
        for what, xs in sorted(st.by.items()):
            xs = sorted(xs)
            print("         %-16s n=%-5d p50 %5.0f ms  p99 %6.0f ms  max %6.0f ms" % (
                what, len(xs), statistics.median(xs), xs[max(0, int(len(xs) * 0.99) - 1)], xs[-1]), flush=True)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    s = sub.add_parser("serve"); s.add_argument("--data", required=True); s.add_argument("--port", type=int, default=13552)
    l = sub.add_parser("load"); l.add_argument("--port", type=int, default=13552)
    l.add_argument("--steps", default="50,150,400"); l.add_argument("--seconds", type=float, default=60)
    a = ap.parse_args()
    if a.mode == "serve":
        asyncio.run(serve(a.data, a.port))
    else:
        asyncio.run(load(a.port, [int(x) for x in a.steps.split(",")], a.seconds))


if __name__ == "__main__":
    main()
