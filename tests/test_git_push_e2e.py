#!/usr/bin/env python3
"""END-TO-END push test through the REAL stack: a real `git push` over HTTP -> git-http-backend ->
receive-pack -> our pre-receive hook -> a read of the maintainer-signed 30618 from THIS node's RELAY
(#161: never its Postgres) -> accept.

This also proves the GRASP_* environment (the relay port, repo root, allow-force) propagates from
git_host_main's Popen env THROUGH git-http-backend + receive-pack into the hook — the hook can't ask
the relay otherwise. The relay is a real websocket server (tests/git_relay_fake.serve) holding the
test's events in memory; nothing is written to any database and the repo store is a temp dir.
"""

import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.services import git_host_service as ghs        # noqa: E402
from app.services.nostr import bip340, nostr_service    # noqa: E402
from app.services.nostr.event import build_event        # noqa: E402
from tests import git_relay_fake                        # noqa: E402

_results = []


def check(name, cond):
    _results.append(bool(cond))
    print("  [%s] %s" % ("PASS" if cond else "FAIL", name))


def _serve(config):
    import git_host_main
    git_host_main._CONFIG = config

    class _S(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True
    httpd = _S(("127.0.0.1", 0), git_host_main._Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    return httpd, port


def _client_commit(workdir, msg):
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    with open(os.path.join(workdir, "f.txt"), "a") as f:
        f.write(msg + "\n")
    subprocess.run(["git", "-C", workdir, "add", "-A"], env=env, check=True, capture_output=True)
    subprocess.run(["git", "-C", workdir, "commit", "-m", msg], env=env, check=True, capture_output=True)
    return subprocess.run(["git", "-C", workdir, "rev-parse", "HEAD"], env=env,
                          capture_output=True, text=True).stdout.strip()


def main():
    if not os.path.exists("/usr/libexec/git-core/git-http-backend"):
        print("git-http-backend missing"); return 2

    del _results[:]
    events = []                                   # the relay's store; appended to as the test goes
    relay, relay_port = git_relay_fake.serve(events)
    tmp = tempfile.mkdtemp(prefix="grasp_e2e_")
    # Via the ENV VAR, never a module attribute: git_project_root() is lazy and reads
    # GRASP_GIT_PROJECT_ROOT / upload_path / a default, so an attribute assignment silently
    # leaves the test writing into the live repo store.
    old_root = os.environ.get("GRASP_GIT_PROJECT_ROOT")
    os.environ["GRASP_GIT_PROJECT_ROOT"] = os.path.join(tmp, "git_repos")
    os.makedirs(os.environ["GRASP_GIT_PROJECT_ROOT"], exist_ok=True)

    owner_sk = secrets.token_bytes(32)
    owner_hex = bip340.pubkey_from_seckey(owner_sk).hex()
    npub = nostr_service.npub_of(owner_hex)
    repo_id = "grasptest" + secrets.token_hex(4)

    config = {"relay_port": relay_port, "repo_root": _ROOT, "repo_max_mb": 512, "allow_force": True,
              "nip98_push": False, "public_base": "", "read_skew": 300, "port": 0}
    httpd, port = _serve(config)
    time.sleep(0.3)

    try:
        ghs.create_repo(owner_hex, repo_id, private=False)

        # Build a client repo + first commit; capture SHA to sign the authorizing 30618.
        work = os.path.join(tmp, "work")
        subprocess.run(["git", "init", "-q", "-b", "main", work], check=True, capture_output=True)
        sha1 = _client_commit(work, "one")
        remote = "http://127.0.0.1:%d/%s/%s.git" % (port, npub, repo_id)
        subprocess.run(["git", "-C", work, "remote", "add", "origin", remote], check=True, capture_output=True)

        # --- REJECT first: push with NO signed 30618 present at all -> hook fail-closed rejects.
        print("1) push with no signed 30618 -> expect REJECT")
        r = subprocess.run(["git", "-C", work, "push", "origin", "main"], capture_output=True, text=True)
        print("   rc=%d; server said: %s" % (r.returncode, (r.stderr.strip().splitlines() or [""])[-1][:120]))
        check("push rejected when no 30618 exists (fail-closed)", r.returncode != 0)
        check("hook actually ran (relay port env propagated through git-http-backend)",
              "no signed" in r.stderr or "30618" in r.stderr or "GRASP" in r.stderr)
        check("...and it ASKED the relay rather than failing to (no 'could not be asked')",
              "could not be asked" not in r.stderr and "no relay configured" not in r.stderr)

        # --- ACCEPT: sign a maintainer 30618 pinning refs/heads/main -> sha1, publish, push.
        print("2) publish maintainer-signed 30618 pinning main->sha1 -> expect ACCEPT")
        events.append(build_event(owner_sk, 30618, "",
                                  tags=[["d", repo_id], ["HEAD", "ref: refs/heads/main"],
                                        ["refs/heads/main", sha1]]))
        r = subprocess.run(["git", "-C", work, "push", "origin", "main"], capture_output=True, text=True)
        if os.environ.get("GRASP_DEBUG"):
            print("   DEBUG full stderr:\n" + r.stderr)
        print("   rc=%d; server said: %s" % (r.returncode, (r.stderr.strip().splitlines() or [""])[-1][:120]))
        check("push ACCEPTED with matching maintainer-signed 30618", r.returncode == 0)
        landed = subprocess.run(["git", "--git-dir", ghs.repo_dir(owner_hex, repo_id),
                                 "rev-parse", "refs/heads/main"], capture_output=True, text=True).stdout.strip()
        check("bare repo now has refs/heads/main == pushed sha", landed == sha1)

        # --- REJECT: a NEW commit whose SHA the signed 30618 does NOT name -> reject (stale state).
        print("3) new commit not named by 30618 -> expect REJECT")
        sha2 = _client_commit(work, "two")
        r = subprocess.run(["git", "-C", work, "push", "origin", "main"], capture_output=True, text=True)
        print("   rc=%d; server said: %s" % (r.returncode, (r.stderr.strip().splitlines() or [""])[-1][:120]))
        check("push of unsigned new SHA rejected", r.returncode != 0)
        still = subprocess.run(["git", "--git-dir", ghs.repo_dir(owner_hex, repo_id),
                                "rev-parse", "refs/heads/main"], capture_output=True, text=True).stdout.strip()
        check("rejected push did NOT move the ref (objects discarded)", still == sha1 and sha2 != sha1)

        # --- REJECT: the RIGHT signed state, but the relay is GONE -> refused, and it says why.
        print("4) sign sha2, then take the relay down -> expect REJECT (fail-closed)")
        events.append(build_event(owner_sk, 30618, "",
                                  tags=[["d", repo_id], ["HEAD", "ref: refs/heads/main"],
                                        ["refs/heads/main", sha2]], created_at=int(time.time()) + 1))
        relay.shutdown()
        r = subprocess.run(["git", "-C", work, "push", "origin", "main"], capture_output=True, text=True)
        print("   rc=%d; server said: %s" % (r.returncode, (r.stderr.strip().splitlines() or [""])[-1][:120]))
        check("push refused when the relay cannot be asked", r.returncode != 0)
        check("...with a message that says the relay could not be asked", "could not be asked" in r.stderr)
        still = subprocess.run(["git", "--git-dir", ghs.repo_dir(owner_hex, repo_id),
                                "rev-parse", "refs/heads/main"], capture_output=True, text=True).stdout.strip()
        check("...and the ref did not move", still == sha1)

    finally:
        httpd.shutdown()
        try:
            relay.shutdown()
        except Exception:
            pass
        if old_root is None:
            os.environ.pop("GRASP_GIT_PROJECT_ROOT", None)
        else:
            os.environ["GRASP_GIT_PROJECT_ROOT"] = old_root
        shutil.rmtree(tmp, ignore_errors=True)

    passed, total = sum(_results), len(_results)
    print("\n%d/%d checks passed" % (passed, total))
    return 0 if passed == total else 1


def test_a_real_push_is_authorized_through_the_relay():
    rc = main()
    if rc == 2:
        pytest.skip("git-http-backend is not installed")
    assert rc == 0 and all(_results), _results


if __name__ == "__main__":
    raise SystemExit(main())
