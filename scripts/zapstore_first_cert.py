"""The asset event zsp is about to publish names Zapstore's pinned certificate FIRST.

relay.zapstore.dev checks only the FIRST `apk_certificate_hash` of an asset (kind 3063) against the certificate it
pinned for the app -- probed 2026-10-25: the pin alone or first passes, the same pin second is refused with "asset
certificate does not match this app". place.poster.app is pinned to the RETIRED signer (eddf3a79...) while the APK
is signed by its successor (c696595f...), so every release from 1.0.2599 on was refused while it led with the active
signer. Reads `zsp publish --offline` output (JSONL) and exits 1 unless every asset's first hash is the pin.

Usage: zapstore_first_cert.py <events.jsonl> <pinned-hash>
"""
import json
import sys


def first_hashes(lines):
    out = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        for ev in parsed if isinstance(parsed, list) else [parsed]:
            if isinstance(ev, dict) and ev.get("kind") == 3063:
                hashes = [t[1].lower() for t in ev.get("tags") or () if len(t) > 1 and t[0] == "apk_certificate_hash"]
                out.append(hashes[0] if hashes else "")
    return out


def main(path, pinned):
    pinned = pinned.strip().lower()
    with open(path) as f:
        firsts = first_hashes(f)
    if not firsts:
        print("::error title=Zapstore pin check::zsp --offline produced no asset event to check")
        return 1
    bad = [h for h in firsts if h != pinned]
    if bad:
        print("::error title=Zapstore pin check::the asset leads with %s, but Zapstore checks only the first "
              "apk_certificate_hash and pins place.poster.app to %s -- it would refuse this release"
              % (bad[0][:16] or "no hash", pinned[:16]))
        return 1
    print("asset leads with the pinned certificate %s (%d asset(s))" % (pinned[:16], len(firsts)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
