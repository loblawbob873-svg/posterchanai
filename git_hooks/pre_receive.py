#!/usr/bin/env python3
"""GRASP pre-receive hook — push authorization (P1, the security crux). FAIL-CLOSED.

Invoked by `git receive-pack` (run under git-http-backend) with one stdin line per ref:
    <old-sha> <new-sha> <refname>
The pushed objects are QUARANTINED by receive-pack; if this process exits non-zero they are
DISCARDED and nothing is written. So an unauthorized push leaves no trace.

Decision (per ref), delegated to app/services/git_auth.decide_push_ref (unit-tested there):
  accept iff a maintainer-signed, signature-re-verified, newest-by-created_at kind-30618 names
  exactly <new-sha> for <refname> — OR a valid NIP-98 header from a maintainer is present
  (the admin/sync.sh convenience path). The maintainer ACL comes ONLY from 30617:<owner>:<id>.

Config is read from the environment set by git_host_main.py (GRASP_*). ANY error -> reject.
"""

import os
import subprocess
import sys

_ROOT = os.environ.get("GRASP_REPO_ROOT") or os.path.abspath(
    os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

ZERO = "0" * 40


def _fail(msg: str):
    sys.stderr.write("GRASP: push rejected — %s\n" % msg)
    sys.exit(1)


def _git_dir() -> str:
    d = os.environ.get("GIT_DIR") or os.getcwd()
    return os.path.realpath(d)


def _owner_repo_from_gitdir(gitdir: str):
    """.../data/git_repos/<owner_hex>/<id>.git -> (owner_hex, repo_id). Traversal-proof: both parts
    are re-validated by git_host_service against GIT_PROJECT_ROOT."""
    base = os.path.basename(gitdir)
    if not base.endswith(".git"):
        return None
    repo_id = base[:-4]
    owner_hex = os.path.basename(os.path.dirname(gitdir))
    from app.services import git_host_service as ghs
    # Re-derive the canonical path and confirm it matches (confinement check).
    canon = ghs.repo_dir(owner_hex, repo_id)
    if not canon or os.path.realpath(canon) != gitdir:
        return None
    return owner_hex, ghs.sanitize_repo_id(repo_id)


def _is_non_fast_forward(gitdir: str, old: str, new: str) -> bool:
    """True if <old> is NOT an ancestor of <new> (history rewrite / force). A branch create
    (old == 0) is fast-forward by definition."""
    if old == ZERO:
        return False
    try:
        r = subprocess.run(["git", "--git-dir", gitdir, "merge-base", "--is-ancestor", old, new],
                           capture_output=True, timeout=30)
        return r.returncode != 0
    except Exception:
        return True   # can't prove fast-forward -> treat as force (conservative)


def _repo_size_mb(gitdir: str) -> float:
    total = 0
    for root, _dirs, files in os.walk(gitdir):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total / (1024 * 1024)


def main():
    lines = sys.stdin.read().splitlines()
    if not lines:
        sys.exit(0)   # nothing to update

    gitdir = _git_dir()
    orp = _owner_repo_from_gitdir(gitdir)
    if not orp:
        _fail("cannot resolve repo from GIT_DIR (path confinement failed)")
    owner_hex, repo_id = orp

    dsn = os.environ.get("GRASP_PG_DSN", "")
    if not dsn:
        _fail("no relay DSN configured (fail-closed)")

    allow_force = os.environ.get("GRASP_ALLOW_FORCE", "1") == "1"
    nip98_enabled = os.environ.get("GRASP_NIP98_ENABLED", "1") == "1"
    try:
        repo_max_mb = float(os.environ.get("GRASP_REPO_MAX_MB", "512"))
    except ValueError:
        repo_max_mb = 512.0

    from app.services import git_auth

    # One short-lived autocommit connection with a statement timeout; a DB failure -> reject.
    try:
        import psycopg2
        conn = psycopg2.connect(dsn, connect_timeout=5)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 5000")
    except Exception as e:
        _fail("relay DB unavailable (%s)" % e)

    try:
        maintainers = git_auth.load_maintainers(conn, owner_hex, repo_id)

        # NIP-98 convenience path (admin/sync.sh): a maintainer-signed header for THIS receive-pack.
        nip98_signer = None
        if nip98_enabled:
            header = os.environ.get("GRASP_NIP98", "")
            needle = "%s.git/git-receive-pack" % repo_id
            nip98_signer = git_auth.verify_nip98(header, "POST", needle, maintainers, max_skew=60,
                                                 require_method=True)

        state_events = git_auth.load_state_events(conn, owner_hex, repo_id, maintainers)

        # Enforce the per-repo size cap once up front (quarantine objects already on disk under
        # $GIT_DIR/objects, so this measures the would-be post-push size).
        if repo_max_mb > 0 and _repo_size_mb(gitdir) > repo_max_mb:
            _fail("repo exceeds size cap of %d MB" % int(repo_max_mb))

        for line in lines:
            parts = line.split()
            if len(parts) != 3:
                _fail("malformed ref line")
            old, new, ref = parts
            nff = _is_non_fast_forward(gitdir, old, new)
            ok, reason = git_auth.decide_push_ref(
                ref, old, new, maintainers, state_events,
                allow_force=allow_force, is_non_fast_forward=nff, nip98_signer=nip98_signer)
            if not ok:
                _fail(reason)
            else:
                sys.stderr.write("GRASP: %s\n" % reason)
    except SystemExit:
        raise
    except Exception as e:
        _fail("internal error (%s)" % e)   # any uncertainty -> reject
    finally:
        try:
            conn.close()
        except Exception:
            pass
    sys.exit(0)


if __name__ == "__main__":
    main()
