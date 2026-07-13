#!/usr/bin/env python3
"""Publish the Zapstore identity proof (NIP-C1, kind 30509) that ties our APK signing certificate to our npub.

Why this exists: `zsp identity --link-key` can't run unattended. It prints a "private key found in environment"
warning and then waits at a confirmation prompt, so in CI it just exits non-zero and the listing keeps telling
users it "cannot reach signer key". zsp's own docs give the automation path — `--offline` emits the SIGNED
event and you publish it yourself (their example pipes it to `nak event`) — which is what this does, without
adding another binary to the build.

Usage: publish_cert_proof.py <zsp-offline-stdout> <zsp-stderr>
Never fails the build: an unlinked certificate is a trust warning on the listing, not a broken app.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys

RELAYS = ["wss://relay.zapstore.dev", "wss://relay.primal.net", "wss://relay.damus.io"]


def _warn(msg: str) -> None:
    print(f"::warning title=Zapstore::{msg}"[:900])


async def _send(event: dict, url: str) -> tuple[str, bool, str]:
    import websockets
    try:
        async with websockets.connect(url, open_timeout=15) as ws:
            await ws.send(json.dumps(["EVENT", event]))
            res = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
            # ["OK", <id>, <accepted:bool>, <message>]
            ok = bool(res[0] == "OK" and len(res) > 2 and res[2])
            return url, ok, (res[3] if len(res) > 3 else "")
    except Exception as e:  # a relay being down must not fail the build
        return url, False, f"{type(e).__name__}: {e}"


async def _main() -> None:
    out_path, err_path = sys.argv[1], sys.argv[2]
    raw = open(out_path, encoding="utf-8", errors="replace").read()
    # zsp prints a certificate banner + the nsec warning on the same stream as the event — take the JSON object.
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        err = ""
        try:
            err = open(err_path, encoding="utf-8", errors="replace").read()
        except OSError:
            pass
        _warn("certificate linking failed: " + (err or raw or "no output").replace("\n", " ")[:300])
        return
    event = json.loads(m.group(0))
    if not event.get("sig"):
        _warn("the certificate proof came back UNSIGNED — is SIGN_WITH an npub rather than an nsec?")
        return

    results = await asyncio.gather(*(_send(event, r) for r in RELAYS))
    for url, ok, msg in results:
        print(f"  {'ok  ' if ok else 'FAIL'} {url} {msg}")
    accepted = [u for u, ok, _ in results if ok]
    if accepted:
        print(f"::notice title=Zapstore::signing certificate linked to the npub "
              f"(kind 30509, cert {(event.get('tags') or [['', '?']])[0][1][:16]}…) "
              f"— accepted by {len(accepted)}/{len(RELAYS)} relays.")
    else:
        _warn("the certificate proof was signed but no relay accepted it: "
              + "; ".join(f"{u}: {m}" for u, _, m in results))


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except Exception as e:  # never break the APK build over a trust badge
        _warn(f"certificate linking errored: {type(e).__name__}: {e}")
