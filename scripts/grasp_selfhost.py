#!/usr/bin/env python3
"""P4 — self-host the posterchanai repo on the built-in GRASP host, IN PARALLEL with Gitea.

This script only PROVISIONS + announces the repo. Keeping its commits in sync with origin is
scripts/grasp_mirror.py (P5), which sync.sh runs on every deploy. Gitea (origin) stays the deploy
backbone either way — the built-in host is a mirror, not a cutover.

What it does (all gated behind git_server_enabled):
  1. creates the bare repo data/git_repos/<operator_hex>/posterchanai.git (if missing),
  2. announces it (30617 + an initial 30618 from current refs) signed by the operator key, to the
     local relay — so the repo is discoverable and clonable over nostr://,
  3. prints the clone URL (commits are then mirrored on every deploy by scripts/grasp_mirror.py).

Usage:  venv-unified/bin/python scripts/grasp_selfhost.py [--repo-id posterchanai] [--public]
Run it on the port-3051 host. Public (announced) by default; pass --private to keep it unannounced.
"""

import argparse
import asyncio
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-id", default="posterchanai")
    ap.add_argument("--private", action="store_true", help="host but do NOT announce (no 30617/30618)")
    ap.add_argument("--name", default="PosterChanAI")
    ap.add_argument("--description", default="Self-hosted PosterChanAI (GRASP mirror of Gitea)")
    args = ap.parse_args()

    from app.services import settings_store
    settings_store.load_local()
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        settings_store.hydrate_from_db(db)
    finally:
        db.close()

    if not settings_store.get_bool("git_server_enabled", False):
        print("git_server_enabled is off — enable the git host first (Admin → Git). Aborting.")
        return 2

    from app.services import keystore
    from app.services.nostr import nostr_service
    op_nsec = keystore.get_operator_nsec()
    if not op_nsec:
        print("no operator key on this node — cannot own the repo. Aborting.")
        return 2
    owner_hex = nostr_service.derive_pubkey(nostr_service.decode_seckey(op_nsec))
    npub = nostr_service.npub_of(owner_hex)

    from app.services import git_host_service as ghs
    res = ghs.create_repo(owner_hex, args.repo_id, private=args.private)
    if not res.get("ok"):
        print("create_repo failed: %s" % res.get("error"))
        return 1
    print("%s bare repo: %s" % ("created" if res.get("created") else "reconciled", res["path"]))

    base = settings_store.get("git_server_public_base", "") or ""
    clone = "%s/%s/%s.git" % (base.rstrip("/"), npub, args.repo_id) if base else \
            "http://127.0.0.1:%s/%s/%s.git" % (settings_store.get("git_server_port", "3053"), npub, args.repo_id)

    if not args.private:
        seckey = nostr_service.decode_seckey(op_nsec)
        from app.services.nostr.event import build_event
        from app.services import nostr_store
        port = settings_store.get_int("nostr_relay_port", 3052)
        a_tags = [["d", args.repo_id], ["name", args.name], ["description", args.description],
                  ["clone", clone], ["maintainers", owner_hex]]
        ann = build_event(seckey, 30617, "", tags=a_tags)
        refs = ghs.repo_refs(owner_hex, args.repo_id)
        head = ghs.repo_head(owner_hex, args.repo_id)
        s_tags = [["d", args.repo_id]]
        if head:
            s_tags.append(["HEAD", "ref: " + head])
        for name, sha in sorted(refs.items()):
            s_tags.append([name, sha])
        s_tags.append(["a", "30617:%s:%s" % (owner_hex, args.repo_id)])
        state = build_event(seckey, 30618, "", tags=s_tags)
        ok1, m1 = asyncio.get_event_loop().run_until_complete(nostr_store.publish_event(port, ann))
        ok2, m2 = asyncio.get_event_loop().run_until_complete(nostr_store.publish_event(port, state))
        print("announced 30617=%s (%s), 30618=%s (%s)" % (ok1, m1, ok2, m2))
    else:
        print("private — NOT announced (no 30617/30618 published)")

    print("\n# To mirror this working tree onto the built-in host (run manually; does NOT touch sync.sh):")
    print("git remote add grasp %s" % clone)
    print("git push grasp master")
    print("\n# Soak alongside Gitea; only consider P5 (cutover) after sustained equivalence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
