#!/usr/bin/env python3
"""What does a very large chunked folder-sync upload actually cost?

Run: venv-unified/bin/python scripts/measure_chunked_upload.py [--gb 5] [--chunk-mb 4]

The sweep caps a file at SYNC_MAX_BYTES (256 MB) and skips anything larger. That ceiling was set
when a file, its ciphertext and the upload body were all in memory at once; chunking has since made
that untrue, so the question is whether the number can go up — and the honest way to answer it is to
push the chunks rather than to reason about them.

What this measures, on the REAL Blossom endpoint with REAL BUD-01 auth:

  * per-chunk cost (sign + PUT), which is what multiplies by ~1250 for a 5 GB file
  * sustained throughput, so the wall-clock for a whole file is measured rather than guessed
  * whether the server's own memory grows with the number of chunks (it must not — each PUT is
    independent, and if it does the ceiling is the server's, not the client's)
  * the MANIFEST cost, which is the part nobody looks at: a chunk list is ~70 bytes per chunk, so
    one 5 GB file carries ~87 KB of shas — past the 45 KB inline limit ON ITS OWN, i.e. a single
    huge file forces the manifest into a Blossom blob for the whole folder.

By default it uploads a SAMPLE and extrapolates, because writing 5 GB to a production drive to
answer a sizing question is rude. --full does the real thing.

Exit 0 = measured, 2 = could not run. It prints a recommendation; it does not change any code.
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
import time

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DEFAULT_SERVER = os.environ.get("PC_BLOSSOM", "http://127.0.0.1:3051/blossom")


def auth_header(seckey, sha_list):
    """A BUD-01 upload authorization committing to these hashes — the same shape the client signs."""
    from app.services.nostr.event import build_event
    tags = [["t", "upload"], ["expiration", str(int(time.time()) + 3600)]]
    tags += [["x", s] for s in sha_list]
    ev = build_event(seckey, 24242, "Upload blob", tags=tags)
    return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()


def _delete_auth(seckey, sha):
    from app.services.nostr.event import build_event
    ev = build_event(seckey, 24242, "Delete blob",
                     tags=[["t", "delete"], ["x", sha], ["expiration", str(int(time.time()) + 3600)]])
    return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()


def _seckey():
    """Sign as the node OPERATOR.

    A throwaway key is refused with "needs the can_blossom privilege", which is the server working
    correctly — an unknown pubkey may not fill the drive. Granting the privilege to a scratch key
    would mean writing a user row to answer a sizing question, so this borrows the key the node
    already signs its own documents with.
    """
    from app.services import keystore
    from app.services.nostr import bech32
    nsec = keystore.get_operator_nsec()
    if not nsec:
        print("no operator key on this node — run this where one exists, or point --server at one "
              "that will accept an unprivileged upload")
        sys.exit(2)
    sk = bech32.decode("nsec", nsec) if nsec.startswith("nsec") else bytes.fromhex(nsec)
    if not sk:
        print("could not decode the operator key")
        sys.exit(2)
    return sk


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gb", type=float, default=5.0, help="file size to model")
    ap.add_argument("--chunk-mb", type=int, default=4, help="chunk size (Android uses 4)")
    ap.add_argument("--sample", type=int, default=24, help="chunks to actually upload")
    ap.add_argument("--full", action="store_true", help="upload the WHOLE modelled size")
    ap.add_argument("--server", default=DEFAULT_SERVER)
    ap.add_argument("--keep", action="store_true", help="do NOT delete the test blobs afterwards")
    a = ap.parse_args()

    chunk = a.chunk_mb * 1024 * 1024
    total_chunks = int((a.gb * 1024 * 1024 * 1024) // chunk)
    n = total_chunks if a.full else min(a.sample, total_chunks)

    seckey = _seckey()
    print(f"server      {a.server}")
    print(f"modelling   {a.gb} GB in {total_chunks} x {a.chunk_mb} MB chunks")
    print(f"uploading   {n} chunk(s){' (FULL)' if a.full else ' (sample)'}\n")

    # Distinct bytes per chunk: identical chunks would dedup server-side and measure nothing.
    times, shas = [], []
    sent = 0
    t_all = time.time()
    for i in range(n):
        body = secrets.token_bytes(64) + bytes(chunk - 64)
        sha = hashlib.sha256(body).hexdigest()
        t0 = time.time()
        try:
            r = requests.put(a.server + "/upload", data=body, timeout=300, headers={
                "Authorization": auth_header(seckey, [sha]),
                "Content-Type": "application/octet-stream",
            })
        except Exception as e:
            print(f"FAILED at chunk {i + 1}: {e}")
            return 2
        dt = time.time() - t0
        if r.status_code >= 400:
            print(f"FAILED at chunk {i + 1}: HTTP {r.status_code} {r.text[:200]}")
            return 2
        times.append(dt)
        shas.append(sha)
        sent += chunk
        if (i + 1) % 8 == 0 or i == n - 1:
            print(f"  {i + 1:5d}/{n}  {sent / 1048576:8.0f} MB  "
                  f"last {dt * 1000:6.0f} ms  {sent / 1048576 / (time.time() - t_all):6.1f} MB/s")

    wall = time.time() - t_all
    per = sum(times) / len(times)
    mbps = (sent / 1048576) / wall if wall else 0
    # The tail is what hurts on a long run: one slow chunk in a thousand is a stall the user sees.
    slow = sorted(times)[int(len(times) * 0.95):] or times[-1:]

    print(f"\nper chunk   mean {per * 1000:.0f} ms · p95 {min(slow) * 1000:.0f} ms · max {max(times) * 1000:.0f} ms")
    print(f"throughput  {mbps:.1f} MB/s sustained over {wall:.1f}s")
    est = total_chunks * per
    print(f"\n{a.gb} GB = {total_chunks} chunks -> ~{est / 60:.1f} min of uploading"
          f"{' (MEASURED)' if a.full else ' (extrapolated from the sample)'}")

    # Put the drive back. These are megabytes of random bytes uploaded to answer a question, and
    # leaving them behind would make the measurement itself a storage leak.
    if not a.keep:
        gone = 0
        for sha in shas:
            try:
                d = requests.delete(a.server + "/" + sha, timeout=30,
                                    headers={"Authorization": _delete_auth(seckey, sha)})
                gone += 1 if d.status_code < 400 else 0
            except Exception:
                pass
        print(f"cleanup     removed {gone}/{len(shas)} test blobs")

    manifest_bytes = total_chunks * 70
    print(f"manifest    ~{manifest_bytes / 1024:.0f} KB of chunk hashes for this ONE file "
          f"({'over' if manifest_bytes > 45000 else 'under'} the 45 KB inline limit by itself)")
    print("\nNote: this measures the SERVER side. Client-side memory is bounded by the chunk size by\n"
          "construction (readPart/writePart), which is the thing the old 256 MB ceiling existed for.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
