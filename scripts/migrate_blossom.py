#!/usr/bin/env python3
"""Import an existing (hzrd149) blossom-server's blobs into PosterChanAI's built-in Blossom.

Run on a node that has BOTH the old blob files and the PosterChanAI DB (e.g. nas.lan).
Blobs are re-stored through the configured backend — which defaults to the storage
**proxy** — so they land on the shared storage server exactly like the rest of PosterChanAI.

Layout it understands (hzrd149 blossom-server local storage):
  * a SQLite db with `blobs(sha256, type, size, uploaded)` + `owners(pubkey, blob, ...)`
  * blob files named `<sha256>` or `<sha256>.<ext>` somewhere under the storage dir.

Usage:
  python scripts/migrate_blossom.py --source-dir /path/to/blossom/data \
      [--sqlite /path/to/blossom/data/sqlite.db] \
      [--owner npub1... | <hex>]   # fallback owner when a blob has no owners row \
      [--ttl-days 0] [--dry-run]

If --sqlite is omitted, every 64-hex-named file under --source-dir is imported with the
--owner pubkey and a guessed mime type.
"""
import argparse
import asyncio
import mimetypes
import os
import sqlite3
import sys

# Run from anywhere: make `import app...` resolve against the repo root.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.database import SessionLocal  # noqa: E402
from app.services import blossom_service  # noqa: E402
from app.services.nostr import nostr_service  # noqa: E402


def _find_file(source_dir: str, sha: str, ext_hint: str = "") -> str | None:
    cands = [os.path.join(source_dir, sha)]
    if ext_hint:
        cands.append(os.path.join(source_dir, f"{sha}.{ext_hint.lstrip('.')}"))
    # hzrd149 sometimes shards by prefix; also try common layouts then fall back to a walk.
    cands.append(os.path.join(source_dir, sha[:2], sha))
    for c in cands:
        if os.path.isfile(c):
            return c
    for root, _dirs, files in os.walk(source_dir):
        for f in files:
            if f == sha or f.split(".", 1)[0] == sha:
                return os.path.join(root, f)
    return None


def _rows_from_sqlite(db_path: str) -> list[dict]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    out = []
    owners: dict[str, str] = {}
    try:
        for r in con.execute("SELECT pubkey, blob FROM owners"):
            owners.setdefault(r["blob"], r["pubkey"])
    except sqlite3.Error:
        pass
    for r in con.execute("SELECT sha256, type, size, uploaded FROM blobs"):
        out.append({
            "sha256": r["sha256"],
            "mime": r["type"] or "",
            "pubkey": owners.get(r["sha256"], ""),
        })
    con.close()
    return out


def _rows_from_dir(source_dir: str) -> list[dict]:
    out = []
    for root, _dirs, files in os.walk(source_dir):
        for f in files:
            stem = f.split(".", 1)[0]
            if len(stem) == 64 and all(c in "0123456789abcdef" for c in stem.lower()):
                out.append({"sha256": stem.lower(), "mime": "", "_file": os.path.join(root, f)})
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source-dir", required=True)
    ap.add_argument("--sqlite", default="")
    ap.add_argument("--owner", default="", help="fallback owner npub/hex")
    ap.add_argument("--ttl-days", type=int, default=None,
                    help="override the global TTL for imported blobs (default: use setting)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    fallback_owner = ""
    if args.owner:
        fallback_owner = nostr_service.to_pubkey_hex(args.owner) or ""
        if not fallback_owner:
            print(f"!! invalid --owner: {args.owner}", file=sys.stderr)
            return 2

    rows = _rows_from_sqlite(args.sqlite) if args.sqlite else _rows_from_dir(args.source_dir)
    print(f"Found {len(rows)} blob(s) to import (backend resolves from settings).")

    db = SessionLocal()
    ok = skipped = failed = 0
    try:
        cfg = blossom_service._cfg(db)
        print(f"Backend: {cfg['backend']}  (storage_url={cfg['storage_url'] or '-'})")
        for row in rows:
            sha = row["sha256"]
            owner = row.get("pubkey") or fallback_owner
            if not owner:
                print(f"  skip {sha[:12]}… (no owner; pass --owner)")
                skipped += 1
                continue
            owner = nostr_service.to_pubkey_hex(owner) or owner
            path = row.get("_file") or _find_file(args.source_dir, sha,
                                                   (mimetypes.guess_extension(row["mime"]) or "").lstrip("."))
            if not path:
                print(f"  MISS {sha[:12]}… (file not found under source-dir)")
                failed += 1
                continue
            with open(path, "rb") as f:
                data = f.read()
            actual = blossom_service.compute_sha256(data)
            if actual != sha:
                print(f"  BAD  {sha[:12]}… (sha mismatch: file is {actual[:12]}…)")
                failed += 1
                continue
            mime = row.get("mime") or mimetypes.guess_type(path)[0] or "application/octet-stream"
            if args.dry_run:
                print(f"  would import {sha[:12]}… ({len(data)} B, {mime}, owner {owner[:12]}…)")
                ok += 1
                continue
            # Apply a TTL override by temporarily honoring the setting; save_blob reads ttl
            # from settings, so for a per-run override we patch the row after the fact.
            await blossom_service.save_blob(db, owner, data, mime)
            if args.ttl_days is not None:
                from app.models import BlossomBlob
                import time as _t
                b = db.query(BlossomBlob).filter(BlossomBlob.sha256 == sha).first()
                if b:
                    b.expires_at = (int(_t.time()) + args.ttl_days * 86400) if args.ttl_days > 0 else None
                    db.commit()
            print(f"  ok   {sha[:12]}… ({len(data)} B, {mime})")
            ok += 1
    finally:
        db.close()

    print(f"\nDone. imported={ok} skipped={skipped} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
