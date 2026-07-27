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
        # A first push has to settle the default branch before anything reads it: `git init --bare`
        # stamped HEAD from the server's init.defaultBranch, which is a branch nobody pushed. Do it
        # BEFORE the witness so the 30618's HEAD tag names the branch that actually exists.
        adopted = ghs.adopt_head_if_unborn(owner_hex, rid)
        if adopted:
            sys.stderr.write("GRASP: default branch set to %s\n" % adopted[len("refs/heads/"):])
        # The operator authors this as a witness/mirror at 30618:<operator>:<id>, distinct from the
        # maintainer's own 30618:<owner>:<id> which stays authoritative for the ACL. Shared with the
        # web-editor commit path (git_host_main) so both produce identical tags — private repos and
        # every failure mode are handled inside.
        ok = ghs.publish_state_witness(owner_hex, rid)
        sys.stderr.write("GRASP: witness 30618 %s\n" % ("published" if ok else "not published"))
    except Exception:
        pass   # best-effort; a push is already committed by the time post-receive runs
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
