#!/usr/bin/env python3
"""Can a remote signer actually talk through this relay?

    venv-unified/bin/python scripts/check_signer_transport.py [ws://127.0.0.1:3052/relay]

NIP-46 signing is a conversation in kind-24133 between two keys, and on this relay it is the one
kind that CANNOT be gated: the client side is an ephemeral app key by construction and Amber signs
with a per-application key, so NEITHER party is necessarily in the web of trust. Gate it on the WoT
and bunker login dies; gate it on "is someone subscribed right now" and a request sent the instant
you hit Post is refused because the phone was dozing. Both of those were shipped once.

And it is EPHEMERAL — nothing is stored — so no query after the fact can tell you whether it worked.
The only honest test is the live round trip this does: subscribe as the signer, publish as the
client with a p-tag, and see whether the relay hands it over.

Both keys are throwaways with no history and no WoT membership, deliberately: that is exactly the
shape of the traffic, and a check run with a trusted key would pass against a relay that rejects
every real signer.

Exit 0 the transport works, 1 it does not, 2 could not run (no relay here).
"""
import asyncio
import json
import os
import secrets
import sys

# Run from anywhere: the checks are launched by test.sh from the repo root and by hand from here.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:3052/relay"


async def main():
    try:
        import websockets
        from app.services.nostr import bip340
        from app.services.nostr import event as E
    except Exception as e:
        print(f"SKIP  cannot import the nostr helpers here ({e})")
        return 2

    sk_client = secrets.token_bytes(32)
    sk_signer = secrets.token_bytes(32)
    pk_signer = bip340.pubkey_from_seckey(sk_signer).hex()

    try:
        sub = await asyncio.wait_for(websockets.connect(URL), 10)
    except Exception as e:
        print(f"SKIP  no relay at {URL} ({type(e).__name__})")
        return 2
    try:
        pub = await asyncio.wait_for(websockets.connect(URL), 10)
    except Exception as e:
        await sub.close()
        print(f"SKIP  no relay at {URL} ({type(e).__name__})")
        return 2

    try:
        # The signer, waiting to be asked.
        await sub.send(json.dumps(["REQ", "sig", {"kinds": [24133], "#p": [pk_signer]}]))
        while True:
            m = json.loads(await asyncio.wait_for(sub.recv(), 15))
            if m[0] == "EOSE":
                break

        ev = E.build_event(sk_client, 24133, "signer-transport-probe", [["p", pk_signer]])
        await pub.send(json.dumps(["EVENT", ev]))
        ok = json.loads(await asyncio.wait_for(pub.recv(), 15))
        if ok[0] != "OK" or not ok[2]:
            why = ok[3] if len(ok) > 3 else ""
            print(f"FAIL  the relay REFUSED a signer frame: {why or ok}")
            print("      Neither party is in the web of trust, and neither can be — that is what a "
                  "remote signer looks like. Refusing it kills bunker login outright.")
            return 1

        try:
            got = json.loads(await asyncio.wait_for(sub.recv(), 15))
        except asyncio.TimeoutError:
            print("FAIL  accepted and never delivered — the signer sees nothing and the client "
                  "waits until it times out, which is 'signing does nothing' with no error.")
            return 1
        if got[0] != "EVENT" or got[2].get("kind") != 24133:
            print(f"FAIL  unexpected frame back: {got[:2]}")
            return 1
    finally:
        await sub.close()
        await pub.close()

    print("OK  a remote signer can talk through this relay (24133 accepted and fanned out)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
