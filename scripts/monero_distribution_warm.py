#!/usr/bin/env python3
"""KEEP MONEROD'S OUTPUT DISTRIBUTION CACHED, SO A SPEND NEVER REBUILDS IT.

Every transaction a wallet builds starts with `/get_output_distribution.bin` for amount 0 over the WHOLE
chain (`from_height 0`, `to_height 0` = the tip) — the decoy selection needs it. monerod answers that from
a cache in `rpc::RpcHandler::get_output_distribution` (src/rpc/rpc_handler.cpp), and the cache is ONE
slot: every amount-0 answer overwrites it, whatever its range. The wallet then makes a SECOND amount-0
request in the same transaction — the pre-fork segregation range (`from ≈ fork - recent`,
`to = fork + 1`, wallet2.cpp `get_outs`) — which overwrites the slot with a range nobody asks for again.

So the NEXT spend misses and monerod rebuilds the distribution from block 0, walking the block table of a
271 GB LMDB under a lock every other RPC call waits on. Measured on nas.lan (2026-09-25):

    full distribution, cached           0.04 s
    after one segregation-range call   14.2 s   (pages still warm)
    cold, after a monerod restart       ~5 min  (a zap: prepare OK, then "check your history";
                                                  the wallet timed out and built nothing)

How long that takes depends on what the page cache happens to hold at the moment somebody presses Send,
which is why it was "perfectly fine" for weeks and then was not.

This asks for exactly the wallet's full-chain request every INTERVAL seconds. While the slot holds it,
that is a cache hit (the 0.04 s above — a block arriving only EXTENDS it). After a spend has evicted it,
this pays the rebuild within INTERVAL seconds, off the path of anybody waiting, and keeps the pages it
reads hot. It changes nothing a wallet does and holds no state: stop it and behaviour is exactly as before.
"""
import json, os, re, sys, time, urllib.request

URL = os.environ.get("MONEROD_RPC", "http://127.0.0.1:18081/json_rpc")
INTERVAL = float(os.environ.get("XMR_WARM_INTERVAL", "10"))
#: A rebuild is minutes when cold; the request must outlive it or it is abandoned half-way and repeated.
TIMEOUT = float(os.environ.get("XMR_WARM_TIMEOUT", "900"))
#: Slower than this is a rebuild, not a cache hit, and is worth a log line.
REBUILD_S = float(os.environ.get("XMR_WARM_REBUILD_S", "1.0"))


def request_params():
    """The wallet's own request (wallet2::get_rct_distribution). The cache key is (amount 0, from_height,
    to_height) and monerod reads `to_height 0` as the tip — any other spelling primes a different entry
    and evicts the one the wallet is about to ask for. `cumulative` is applied after the lookup and
    `binary`/`compress` only shape the reply, so they are chosen to keep the reply small."""
    return {"amounts": [0], "from_height": 0, "to_height": 0,
            "cumulative": False, "binary": True, "compress": True}


def warm_once(url=URL, timeout=TIMEOUT):
    """(ok, seconds, detail). Never raises: a daemon that is down or busy is a reason to try again."""
    body = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "get_output_distribution",
                       "params": request_params()}).encode()
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except Exception as e:  # connection refused, timeout — "could not ask"
        return False, time.monotonic() - t0, f"{type(e).__name__}: {e}"
    dt = time.monotonic() - t0
    return (True, dt, "ok") if reply_ok(raw) else (False, dt, f"unexpected reply: {raw[-200:]!r}")


def reply_ok(raw):
    """With `binary` on, monerod writes the distribution as RAW compressed bytes inside the JSON string —
    not valid UTF-8, so the reply cannot be json-decoded (measured: UnicodeDecodeError at byte 579642).
    The top-level `status` is the LAST one in the body (it follows `distributions`); an error reply has
    no result status at all."""
    found = re.findall(rb'"status"\s*:\s*"([^"]*)"', raw)
    return bool(found) and found[-1] == b"OK" and b'"distributions"' in raw


def main(argv=None):
    once = "--once" in (argv if argv is not None else sys.argv[1:])
    while True:
        ok, dt, detail = warm_once()
        if not ok:
            print(f"[warm] could not ask monerod ({detail}) after {dt:.1f}s", flush=True)
        elif dt >= REBUILD_S:
            # A rebuild: a spend (or a restart) had evicted the slot. Seeing these is expected; seeing one
            # take minutes says the chain is being read from disk.
            print(f"[warm] rebuilt the output distribution in {dt:.1f}s", flush=True)
        if once:
            return 0 if ok else 1
        time.sleep(INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
