#!/usr/bin/env python3
"""GRASP post-receive hook — publish a normalized kind-30618 witness (P1, belt-and-suspenders).

After a push is accepted (pre-receive exited 0 and the refs moved), re-publish a kind-30618 state
event reflecting the ACTUAL on-disk refs, signed by the HOST OPERATOR key, to the LOCAL relay only
(ws://127.0.0.1:3052 — never public DEFAULT_RELAYS, per the bots-local-relay rule). This is a MIRROR:
the maintainer's own 30618 remains the ACL authority in pre-receive; the operator witness just keeps
the relay's advertised state matching reality if the client's pre-published 30618 drifted.

Best-effort: this NEVER fails the push (always exit 0). PRIVATE repos are skipped entirely — a
private repo must not publish any public 30617/30618.
"""

import os
import sys

_ROOT = os.environ.get("GRASP_REPO_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _git_dir() -> str:
    return os.path.realpath(os.environ.get("GIT_DIR") or os.getcwd())


def main():
    try:
        # Drain stdin (git feeds the ref lines) so git-receive-pack doesn't see a broken pipe.
        try:
            sys.stdin.read()
        except Exception:
            pass

        gitdir = _git_dir()
        base = os.path.basename(gitdir)
        if not base.endswith(".git"):
            return
        repo_id = base[:-4]
        owner_hex = os.path.basename(os.path.dirname(gitdir))

        from app.services import git_host_service as ghs
        rid = ghs.sanitize_repo_id(repo_id)
        if not rid or ghs.repo_dir(owner_hex, rid) is None:
            return
        if ghs.is_private(owner_hex, rid):
            return   # private repo -> never announce/witness publicly

        refs = ghs.repo_refs(owner_hex, rid)      # {refname: sha}
        head = ghs.repo_head(owner_hex, rid)      # e.g. "refs/heads/main"

        from app.services import keystore
        op_nsec = keystore.get_operator_nsec()
        if not op_nsec:
            return
        from app.services.nostr import nostr_service
        from app.services.nostr.event import build_event
        seckey = nostr_service.decode_seckey(op_nsec)

        # NIP-34 30618 tags: d=repo-id, HEAD ref, then each ref->sha. The operator authors this as a
        # witness/mirror; the addressable coordinate is 30618:<operator>:<id> (distinct from the
        # maintainer's own 30618:<owner>:<id>, which stays authoritative for the ACL).
        tags = [["d", rid]]
        if head:
            tags.append(["HEAD", "ref: " + head])
        for name, sha in sorted(refs.items()):
            tags.append([name, sha])
        tags.append(["a", "30617:%s:%s" % (owner_hex, rid)])   # link back to the announcement

        ev = build_event(seckey, 30618, "", tags=tags)

        import asyncio
        from app.services import nostr_store
        port = int(os.environ.get("GRASP_RELAY_PORT", "3052"))
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            ok, msg = loop.run_until_complete(nostr_store.publish_event(port, ev, timeout=6.0))
            loop.close()
            sys.stderr.write("GRASP: witness 30618 %s (%s)\n" % ("published" if ok else "not published", msg))
        except Exception as e:
            sys.stderr.write("GRASP: witness publish skipped (%s)\n" % e)
    except Exception:
        pass   # best-effort; a push is already committed by the time post-receive runs
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
