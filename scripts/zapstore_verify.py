#!/usr/bin/env python3
"""Is this release's APK asset actually STORED on Zapstore's AppCatalog relay? Exit 0 yes, 1 no, 2 could not ask.

zsp prints success when the relay ACCEPTS the asset event, but relay.zapstore.dev only SAVES a kind-3063 asset once
its blob is on cdn.zapstore.dev ("the event will be saved when the referenced blob is uploaded"). 2026-10-10:
1.0.2505's CDN upload timed out, the job re-ran, zsp reused the signed events without re-uploading, and CI printed
"Published 1.0.2505 to Zapstore" while Zapstore never had an installable 2505. Only reading it back proves it.

    zapstore_verify.py <npub-or-hex> <version> [--relay wss://relay.zapstore.dev] [--wait 90]
"""
import argparse
import asyncio
import json
import sys
import time

KIND_ASSET = 3063
KIND_RELEASE = 30063
# 2026-10-27, Zapstore: "There is no more immediate publishing in the new relay, it takes a few minutes max. Events are
# now verified and only then promoted" (orphan 3063s refused, the APK's certificate checked against the C1 proof). An
# asset can be held while its RELEASE is refused -- as the lineage refusal of 2026-10-26 was -- so `--release <app>`
# also requires the promoted kind-30063 `<app>@<version>`.


def _hex(pk: str) -> str:
    pk = pk.strip()
    if pk.startswith("npub1"):
        CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
        data = [CHARSET.find(c) for c in pk[5:]][:-6]
        acc, bits, out = 0, 0, bytearray()
        for v in data:
            acc = (acc << 5) | v
            bits += 5
            while bits >= 8:
                bits -= 8
                out.append((acc >> bits) & 0xFF)
        return out.hex()
    return pk.lower()


async def _assets(relay: str, author: str, timeout: float, flt: dict | None = None) -> list:
    import websockets
    out = []
    async with websockets.connect(relay, open_timeout=timeout, close_timeout=2, max_size=8_000_000) as ws:
        await ws.send(json.dumps(["REQ", "zv", flt or {"kinds": [KIND_ASSET], "authors": [author], "limit": 50}]))
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if m[0] == "EVENT" and m[1] == "zv":
                out.append(m[2])
            elif m[0] in ("EOSE", "CLOSED") and m[1] == "zv":
                if m[0] == "CLOSED":
                    raise RuntimeError("relay closed the query: %s" % (m[2:] or ""))
                return out


def release_filter(author: str, app: str, version: str) -> dict:
    return {"kinds": [KIND_RELEASE], "authors": [author], "#d": ["%s@%s" % (app, version)]}


def has_release(events: list, app: str, version: str) -> bool:
    d = "%s@%s" % (app, version)
    return any(e.get("kind") == KIND_RELEASE and any(t[:2] == ["d", d] for t in (e.get("tags") or []))
               for e in events)


def has_version(events: list, version: str) -> bool:
    return any(t[0] == "version" and t[1] == version
               for e in events for t in (e.get("tags") or []) if isinstance(t, list) and len(t) >= 2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pubkey")
    ap.add_argument("version")
    ap.add_argument("--relay", default="wss://relay.zapstore.dev")
    ap.add_argument("--wait", type=float, default=90.0, help="keep asking this long (the save follows the upload)")
    ap.add_argument("--release", default="", help="APP: require the PROMOTED release <APP>@<version> (kind 30063)")
    a = ap.parse_args(argv)
    author, end, asked = _hex(a.pubkey), time.monotonic() + a.wait, False
    while True:
        try:
            if a.release:
                evs = asyncio.run(_assets(a.relay, author, 20, release_filter(author, a.release, a.version)))
                asked = True
                if has_release(evs, a.release, a.version):
                    print("Zapstore promoted the %s release" % a.version)
                    return 0
            else:
                evs = asyncio.run(_assets(a.relay, author, 20))
                asked = True
                if has_version(evs, a.version):
                    print("Zapstore holds the %s asset" % a.version)
                    return 0
        except Exception as e:      # noqa: BLE001
            print("could not ask %s: %s" % (a.relay, e))
        if time.monotonic() >= end:
            break
        time.sleep(10)
    if not asked:
        return 2
    print(("Zapstore did NOT promote the %s release (refused or still unverified)" if a.release else
           "Zapstore does NOT hold an asset for %s (the release is not installable from Zapstore)") % a.version)
    return 1


if __name__ == "__main__":
    sys.exit(main())
