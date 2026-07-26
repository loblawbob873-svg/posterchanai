#!/usr/bin/env python3
"""END-TO-END push test through the REAL stack: a real `git push` over HTTP -> git-http-backend ->
receive-pack -> our pre-receive hook -> real Postgres read of the maintainer-signed 30618 -> accept.

This also proves the GRASP_* environment (DSN, repo root, allow-force) propagates from git_host_main's
Popen env THROUGH git-http-backend + receive-pack into the hook — the hook can't read the DB otherwise.

Inserts a handful of clearly-namespaced test events into the relay's `events`/`event_tags` and
DELETES them (and the temp repo) in finally. Uses a random repo id so it can't collide.
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import psycopg2
from app.services import git_host_service as ghs
from app.services.nostr import bip340, nostr_service
from app.services.nostr.event import build_event

DSN = "host=127.0.0.1 port=5432 dbname=posterchan_relay user=posterchan"
_results = []
_inserted_ids = []


def check(name, cond):
    _results.append(bool(cond))
    print("  [%s] %s" % ("PASS" if cond else "FAIL", name))


def _insert_event(conn, ev):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO events (id,pubkey,created_at,kind,content,tags,sig,raw,origin) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'direct') ON CONFLICT (id) DO NOTHING",
            (ev["id"], ev["pubkey"], ev["created_at"], ev["kind"], ev["content"],
             json.dumps(ev["tags"]), ev["sig"], json.dumps(ev)))
        for t in ev["tags"]:
            if len(t) >= 2 and isinstance(t[0], str) and len(t[0]) == 1:
                cur.execute("INSERT INTO event_tags (event_id,tag,value) VALUES (%s,%s,%s) "
                            "ON CONFLICT DO NOTHING", (ev["id"], t[0], str(t[1])))
    conn.commit()
    _inserted_ids.append(ev["id"])


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
        print("git-http-backend missing"); return 1

    conn = psycopg2.connect(DSN)
    tmp = tempfile.mkdtemp(prefix="grasp_e2e_")
    ghs.GIT_PROJECT_ROOT = os.path.join(tmp, "git_repos")
    os.makedirs(ghs.GIT_PROJECT_ROOT, exist_ok=True)

    owner_sk = secrets.token_bytes(32)
    owner_hex = bip340.pubkey_from_seckey(owner_sk).hex()
    npub = nostr_service.npub_of(owner_hex)
    repo_id = "grasptest" + secrets.token_hex(4)

    config = {"pg_dsn": DSN, "repo_root": _ROOT, "repo_max_mb": 512, "allow_force": True,
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
        check("hook actually ran (DSN env propagated through git-http-backend)",
              "GRASP" in r.stderr or "authorized" in r.stderr or "signed" in r.stderr)

        # --- ACCEPT: sign a maintainer 30618 pinning refs/heads/main -> sha1, insert, push.
        print("2) insert maintainer-signed 30618 pinning main->sha1 -> expect ACCEPT")
        st = build_event(owner_sk, 30618, "",
                         tags=[["d", repo_id], ["HEAD", "ref: refs/heads/main"], ["refs/heads/main", sha1]])
        _insert_event(conn, st)
        if os.environ.get("GRASP_DEBUG"):
            c2 = psycopg2.connect(DSN); c2.autocommit = True
            from app.services import git_auth as _ga
            m = _ga.load_maintainers(c2, owner_hex, repo_id)
            se = _ga.load_state_events(c2, owner_hex, repo_id, m)
            print("   DEBUG maintainers=%s state_events=%d sha1=%s" % ([x[:8] for x in m], len(se), sha1))
            if se:
                print("   DEBUG refs_from_state=%s" % _ga.refs_from_state(_ga.select_authorized_state(se, m)))
            c2.close()
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

    finally:
        httpd.shutdown()
        try:
            with conn.cursor() as cur:
                for eid in _inserted_ids:
                    cur.execute("DELETE FROM event_tags WHERE event_id=%s", (eid,))
                    cur.execute("DELETE FROM events WHERE id=%s", (eid,))
            conn.commit()
            print("cleanup: removed %d test event(s) from the relay DB" % len(_inserted_ids))
        finally:
            conn.close()
        shutil.rmtree(tmp, ignore_errors=True)

    passed, total = sum(_results), len(_results)
    print("\n%d/%d checks passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
