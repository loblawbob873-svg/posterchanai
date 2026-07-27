#!/usr/bin/env python3
"""Restore a user's Files folder index (`pcai:files-index`) from a rescued encrypted index blob.

Why this exists: the index is ONE replaceable kind-30078 doc. A browser holding the empty default
(fresh device / cleared storage / an index blob it couldn't fetch) used to be able to save over it,
replacing every folder and filename with nothing while the blobs themselves sat untouched in Blossom.
The client-side causes are fixed (FilesIdx._pullOk / _pullBlocked, and the orphan-blob GC now waits
for a pull that actually loaded the index) — this script is the recovery for a drive already wiped.

The rescued blob is AES-256-GCM (12-byte IV prefix) under the client's MASTER key, which is NIP-44
self-wrapped to the user's LOGIN key and therefore only obtainable from their browser:

    // in the /client page console, on a device that still has it
    const k = 'pc_files_idx_<their pubkey hex>';
    JSON.parse(await __PC.nip44dec('<their pubkey hex>', localStorage.getItem(k + '_mk'))).k

That prints the master key as base64url — pass it as --mk. Alternatively, if that device still holds
the whole index, `localStorage.getItem(k)` IS the plaintext index: pass it with --json and no key.

Writing goes through the user's SERVER-HELD storage key (the same key the app uses for this doc), so
no signer is needed on this end.

    python scripts/restore_files_index.py --user 1 --blob ~/rec/index.enc --mk <base64url>
    python scripts/restore_files_index.py --user 1 --json ~/rec/index.json
    python scripts/restore_files_index.py --user 1 --blob ... --mk ... --dry-run
"""
import os
import sys
import json
import base64
import asyncio
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _b64u(s: str) -> bytes:
    s = s.strip().replace("-", "+").replace("_", "/")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def _decrypt(blob: bytes, mk: bytes) -> dict:
    """AES-256-GCM, IV in the first 12 bytes — the exact shape _masterEncrypt writes in app.js."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    plain = AESGCM(mk).decrypt(blob[:12], blob[12:], None)
    return json.loads(plain.decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", type=int, default=1, help="User.id whose index to restore")
    ap.add_argument("--blob", help="rescued encrypted index blob (files-index.enc)")
    ap.add_argument("--mk", help="master key, base64url (see the module docstring)")
    ap.add_argument("--json", dest="jsonfile", help="plaintext index JSON instead of --blob/--mk")
    ap.add_argument("--dry-run", action="store_true", help="show what would be written, write nothing")
    args = ap.parse_args()

    if args.jsonfile:
        idx = json.load(open(args.jsonfile))
    elif args.blob and args.mk:
        idx = _decrypt(open(args.blob, "rb").read(), _b64u(args.mk))
    else:
        ap.error("need --json, or both --blob and --mk")

    files = idx.get("files") or {}
    folders = idx.get("folders") or []
    if not isinstance(files, dict) or not isinstance(folders, list):
        sys.exit("that doesn't look like a files index (no files{} / folders[])")
    print(f"recovered index: {len(folders)} folders, {len(files)} files")
    print("folders:", ", ".join(map(str, folders)))

    from app.services import settings_store as S
    S.load_local()
    from app.database import SessionLocal
    from app.models import User
    from app.services import nostr_store as store

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == args.user).first()
        if not user:
            sys.exit(f"no user with id {args.user}")
        sk = store.user_storage_seckey(db, user)
        port = S.get_int("nostr_relay_port", 3052)

        cur = asyncio.run(store.get_doc(port, "pcai:files-index", seckey=sk))
        cur_files = len((cur or {}).get("files") or {}) if isinstance(cur, dict) else 0
        print(f"current server index: {cur_files} files "
              f"({(cur or {}).get('folders') if isinstance(cur, dict) else 'none'})")
        if cur_files > len(files):
            # Refusing here is the whole lesson of the incident: never let a smaller index replace a
            # larger one without the operator saying so out loud.
            sys.exit("REFUSING: the server index is LARGER than the one you're restoring. "
                     "Re-check the rescued file (or delete this guard deliberately).")
        if args.dry_run:
            print("dry run — nothing written")
            return

        # Write the v1 INLINE shape (folders/files/encFolders). The client re-splits it into an
        # encrypted blob on the next save if it grows past its inline threshold.
        doc = {"folders": folders, "files": files, "encFolders": idx.get("encFolders") or []}
        if idx.get("mk"):
            doc["mk"] = idx["mk"]
        ok = asyncio.run(store.put_doc(port, sk, "pcai:files-index", doc))
        print("restored" if ok else "FAILED to publish the doc")
    finally:
        db.close()


if __name__ == "__main__":
    main()
