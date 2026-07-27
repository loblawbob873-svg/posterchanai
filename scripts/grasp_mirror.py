#!/usr/bin/env python3
"""P5 — mirror this checkout's commits onto the built-in GRASP host (git-over-nostr).

Companion to `grasp_selfhost.py`, which PROVISIONS + announces the repo (30617). This script only
keeps the hosted repo's commits in sync: publish a maintainer-signed kind-30618 naming the new tip,
then `git push` it over smart-HTTP. `sync.sh` calls it on every deploy, so the nostr repo tracks
`origin` instead of drifting (it sat 8 commits stale before this existed).

Gitea (`origin`) remains the DEPLOY backbone — this is a MIRROR, exactly like the `github` remote.
It is best-effort by construction: every failure path prints a `[grasp]` line and the deploy carries
on (sync.sh wraps the call in `|| echo WARN`).

WHERE IT RUNS: on the node that HOSTS the repo. Push authorization is a Nostr signature, not a
connection — only a maintainer of `30617:<owner>:<id>` can authorize a ref update, and on the hosting
node the operator key IS the repo owner (hence always a maintainer, per
`git_auth.load_maintainers`). It also has to be the hosting node's relay that receives the 30618:
the `pre-receive` hook reads THAT node's relay Postgres (GRASP_PG_DSN), so a state event published to
some other node's relay wouldn't be seen in time. A proxy node (`git_server_proxy_url` set) therefore
SKIPS with a message rather than failing — see the sync.sh wiring.

Auth belt-and-braces: the push carries BOTH proofs — the pre-published 30618 (the canonical GRASP
path, which also keeps the relay's advertised state correct) and a NIP-98 header
(`git_server_nip98_push`, the automation path). Either one alone authorizes the push, so a slow relay
write can't wedge a deploy.

Usage:  venv/bin/python scripts/grasp_mirror.py [--repo-id posterchanai] [--branch master]
                                                [--owner <npub|hex>] [--dry-run]
Exit: 0 = mirrored / already current / deliberately skipped;  1 = tried and failed.
"""

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _say(msg: str):
    print("[grasp] %s" % msg, flush=True)


def _git(*args: str) -> tuple[int, str]:
    """Run git in THIS checkout (the source of the mirror)."""
    r = subprocess.run(["git", "-C", _ROOT, *args], capture_output=True, text=True, timeout=600)
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-id", default="posterchanai")
    ap.add_argument("--branch", default="master", help="local branch to mirror (pushed to the same name)")
    ap.add_argument("--owner", default="", help="repo owner npub/hex (default: this node's operator key)")
    ap.add_argument("--dry-run", action="store_true", help="report what would be pushed; publish/push nothing")
    # Realigning the mirror after somebody rewrote already-pushed history (an amend/rebase) is the
    # ONLY reason to force. It is opt-in and never used by sync.sh: a mirror that force-pushes on
    # its own would quietly paper over exactly the divergence you want to be told about.
    ap.add_argument("--force", action="store_true", help="force-update the remote ref (after a history rewrite)")
    args = ap.parse_args()

    from app.services import settings_store
    settings_store.load_local()
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        settings_store.hydrate_from_db(db)
    finally:
        db.close()

    # --- topology gates (skip, never fail: a mirror must not be able to break a deploy) ----------
    proxy = (settings_store.get("git_server_proxy_url", "") or "").strip()
    if proxy:
        _say("this node proxies /git/ to %s — mirror runs on the HOSTING node; skipping." % proxy)
        return 0
    if not settings_store.get_bool("git_server_enabled", False):
        _say("git_server_enabled is off on this node — nothing to mirror to; skipping.")
        return 0

    from app.services import keystore
    from app.services.nostr import nostr_service
    op_nsec = keystore.get_operator_nsec()
    if not op_nsec:
        _say("no operator key on this node — cannot sign the repo state; skipping.")
        return 0
    seckey = nostr_service.decode_seckey(op_nsec)
    operator_hex = nostr_service.derive_pubkey(seckey)

    from app.services import git_host_service as ghs
    # Owner resolution, in order: --owner, then the `grasp_mirror_owner` setting, then this node's
    # operator key. The setting exists because the repo can be owned by a HUMAN npub while the node
    # only holds a maintainer key: the owner pubkey is the clone-URL path segment, so re-owning a repo
    # to its author changes which directory the mirror must push to. Defaulting to the operator key
    # would then silently keep updating the node-owned copy and let the real repo go stale — which is
    # exactly the drift this script exists to prevent.
    _cfg_owner = (settings_store.get("grasp_mirror_owner", "") or "").strip()
    _want = args.owner or _cfg_owner
    owner_hex = ghs.owner_hex_from_npub(_want) if _want else operator_hex
    if not owner_hex:
        _say("owner %r is not a valid npub/hex" % _want)
        return 1
    npub = nostr_service.npub_of(owner_hex)

    repo_id = ghs.sanitize_repo_id(args.repo_id)
    if not repo_id:
        _say("invalid --repo-id %r" % args.repo_id)
        return 1
    if not ghs.repo_exists(owner_hex, repo_id):
        # Provisioning + the 30617 announcement (name/description/clone URL) belong to
        # grasp_selfhost.py; creating it here would publish a default announcement over whatever an
        # operator configured. Point at the right tool instead.
        _say("repo %s/%s is not hosted here — run scripts/grasp_selfhost.py first; skipping."
             % (npub[:12], repo_id))
        return 0

    # --- what to mirror --------------------------------------------------------------------------
    ref = "refs/heads/%s" % args.branch
    rc, new_sha = _git("rev-parse", "--verify", "%s^{commit}" % ref)
    if rc != 0 or len(new_sha) != 40:
        _say("no local %s in this checkout (%s); skipping." % (ref, new_sha[:120]))
        return 0

    hosted = ghs.repo_refs(owner_hex, repo_id)
    if hosted.get(ref) == new_sha:
        _say("already current: %s @ %s" % (ref, new_sha[:12]))
        return 0
    _say("mirroring %s %s -> %s (%s/%s)"
         % (ref, (hosted.get(ref) or "new")[:12], new_sha[:12], npub[:12], repo_id))
    if args.dry_run:
        return 0

    # --- (1) publish the maintainer-signed 30618 that authorizes the new tip ----------------------
    # The hook accepts a ref update only when the newest maintainer-signed 30618 names EXACTLY this
    # sha for this ref, so carry the hosted repo's other refs forward unchanged and move only ours.
    from app.services.nostr.event import build_event
    from app.services import nostr_store
    refs = dict(hosted)
    refs[ref] = new_sha
    head = ghs.repo_head(owner_hex, repo_id) or ref
    tags = [["d", repo_id], ["HEAD", "ref: " + head]]
    tags += [[name, sha] for name, sha in sorted(refs.items())]
    tags.append(["a", "30617:%s:%s" % (owner_hex, repo_id)])
    state = build_event(seckey, 30618, "", tags=tags)
    port = settings_store.get_int("nostr_relay_port", 3052)
    try:
        ok, msg = asyncio.run(nostr_store.publish_event(port, state))
    except Exception as e:               # relay down/unreachable — the NIP-98 header still authorizes
        ok, msg = False, str(e)
    _say("30618 state published=%s (%s)" % (ok, msg))

    # --- (2) push over smart-HTTP, carrying a NIP-98 header as the second proof -------------------
    host_port = settings_store.get("git_server_port", "3053") or "3053"
    url = "http://127.0.0.1:%s/%s/%s.git" % (host_port, npub, repo_id)
    push_url = "%s/git-receive-pack" % url
    nip98 = build_event(seckey, 27235, "", tags=[["u", push_url], ["method", "POST"]])
    header = "Authorization: Nostr %s" % base64.b64encode(
        json.dumps(nip98, separators=(",", ":")).encode()).decode()

    refspec = ("+%s:%s" if args.force else "%s:%s") % (ref, ref)
    rc, out = _git("-c", "http.extraHeader=%s" % header, "push", url, refspec)
    if rc != 0:
        _say("push FAILED (rc=%s): %s" % (rc, out[-800:]))
        return 1
    _say("mirrored %s @ %s -> %s" % (ref, new_sha[:12], url))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
