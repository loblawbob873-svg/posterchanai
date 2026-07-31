#!/usr/bin/env python3
"""Browse API + web-editor + chunked-push verification for the GRASP git host.

Proves, WITHOUT Postgres (pg_dsn="" -> the maintainer ACL is just the URL owner):

  READ  1. /refs lists branches + tags and names the default branch;
        2. /tree, /log and /raw honour `?ref=` — the only way to reach a branch whose NAME contains a
           slash (refs/heads/feature/x), which a single path segment cannot express;
        3. /commit/<sha> returns per-file stats + patch, including for the ROOT commit (no parent) and
           a MERGE commit (diffed against its first parent);
        4. /download serves an attachment, and 404s a missing path;
        5. hostile refs/paths (`--option`, `..`, `@{1}`) are refused, never passed to git.

  WRITE 6. /edit refuses: no header, a header bound to ANOTHER repo, a READ-scoped header (no /edit),
           and a signer outside the maintainer ACL;
        7. /edit with a maintainer's NIP-98 commits the change, records the signer as the author,
           returns the 30618 tags for the client to sign, keeps the exec bit, and deletes files;
        8. `base` is a compare-and-swap: a stale base -> 409, so two editors can't clobber each other;
        9. a path with `..`, `.git/` or a control character is refused.

  PUSH 10. a pack pushed with `Transfer-Encoding: chunked` (what git uses once a pack exceeds
           http.postBuffer) is accepted, lands the right sha, and leaves the repo fsck-clean —
           the `400 Bad request syntax` bug that made every first full push fail.

  ALIAS 11. a clone URL written under a MAINTAINER's npub (ngit derives one per 30617 maintainer, so
            only the owner's path exists on disk) resolves to the owner's repo — and grants nothing:
            without a DSN to confirm the ACL it stays a 404, a non-maintainer npub stays a 404, and
            delete stays owner-only through the aliased URL.

Run: python tests/test_git_host_browse_edit.py   (non-zero exit if any case fails)
"""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_STORE = tempfile.mkdtemp(prefix="grasp-browse-test-")
os.environ["GRASP_GIT_PROJECT_ROOT"] = _STORE      # must be set BEFORE git_host_service resolves it

from app.services.nostr import nostr_service                      # noqa: E402
from app.services.nostr.event import build_event                   # noqa: E402

_results = []


def check(name, cond, extra=""):
    ok = bool(cond)
    _results.append(ok)
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           "" if ok else "   -> %s" % str(extra)[:300]))


def _git(*args, cwd=None, check_rc=True, **kw):
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, **kw)
    if check_rc and p.returncode != 0:
        raise RuntimeError("git %s failed: %s" % (args[:2], p.stderr.decode()[:300]))
    return p


def main():
    sk = os.urandom(32)
    owner = nostr_service.derive_pubkey(sk)
    repo = os.path.join(_STORE, owner, "demo.git")
    os.makedirs(os.path.dirname(repo), exist_ok=True)
    _git("init", "-q", "--bare", repo)
    _git("--git-dir", repo, "config", "http.receivepack", "true")   # create_repo does this in prod

    wt = tempfile.mkdtemp(prefix="grasp-wt-")
    def w(*a, **kw):
        return _git("-C", wt, *a, **kw)
    _git("init", "-q", "-b", "master", wt)
    w("config", "user.email", "t@t"); w("config", "user.name", "t")
    open(os.path.join(wt, "README.md"), "w").write("# demo\n")
    os.makedirs(os.path.join(wt, "src"), exist_ok=True)
    open(os.path.join(wt, "src/app.py"), "w").write("print('hi')\n")
    open(os.path.join(wt, "run.sh"), "w").write("#!/bin/sh\necho a\n")
    os.chmod(os.path.join(wt, "run.sh"), 0o755)
    w("add", "-A"); w("commit", "-qm", "first commit")
    open(os.path.join(wt, "src/app.py"), "w").write("print('hello')\n")
    w("add", "-A"); w("commit", "-qm", "second: tweak app")
    w("checkout", "-qb", "feature/x")
    open(os.path.join(wt, "n.txt"), "w").write("n\n")
    w("add", "-A"); w("commit", "-qm", "feature work")
    w("checkout", "-q", "master")
    w("merge", "-q", "--no-ff", "feature/x", "-m", "merge feature/x")
    w("tag", "v1")
    w("remote", "add", "o", repo)
    w("push", "-q", "o", "master", "feature/x", "v1")

    import git_host_main as gh
    gh._CONFIG = {"pg_dsn": "", "read_skew": 300, "write_skew": 120, "port": 0,
                  "repo_max_mb": 512, "allow_force": True, "nip98_push": True, "public_base": ""}

    class _S(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = _S(("127.0.0.1", 0), gh._Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    base = "http://127.0.0.1:%d/%s/demo.git" % (port, owner)

    def get(path, raw=False):
        try:
            r = urllib.request.urlopen(base + path, timeout=20)
            body = r.read()
            return r.status, (body if raw else json.loads(body)), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")[:200], dict(e.headers)

    def post(path, obj, auth=None):
        req = urllib.request.Request(base + path, data=json.dumps(obj).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        if auth:
            req.add_header("Authorization", auth)
        try:
            r = urllib.request.urlopen(req, timeout=30)
            return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, raw.decode("utf-8", "replace")[:200]

    def nip98(url, secret=None):
        ev = build_event(secret or sk, 27235, "", tags=[["u", url], ["method", "POST"]])
        return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()

    try:
        print("1) refs (branches + tags)")
        st, j, _ = get("/refs")
        check("refs 200", st == 200, (st, j))
        check("branches listed", sorted(b["name"] for b in j["branches"]) == ["feature/x", "master"], j)
        check("tags listed", [t["name"] for t in j["tags"]] == ["v1"], j["tags"])
        check("default branch named", j["default"] == "master", j)

        print("2) tree / log / raw with ?ref= (slashed branch names)")
        st, j, _ = get("/tree/HEAD")
        check("tree lists root", st == 200 and {e["name"] for e in j["entries"]} ==
              {"README.md", "run.sh", "src", "n.txt"}, j)
        st, j, _ = get("/tree/HEAD/src")
        check("tree lists subdir", st == 200 and [e["name"] for e in j["entries"]] == ["app.py"], j)
        st, j, _ = get("/log/HEAD?ref=feature%2Fx&limit=5")
        check("log on slashed ref", st == 200 and j["commits"][0]["subject"] == "feature work", j)
        st, b, _ = get("/raw/HEAD/README.md", raw=True)
        check("raw file bytes", st == 200 and b.startswith(b"# demo"), b[:40])

        print("3) commit diff (normal, root, merge)")
        commits = get("/log/HEAD?limit=20")[1]["commits"]
        tweak = next(c for c in commits if c["subject"].startswith("second"))
        st, j, _ = get("/commit/" + tweak["sha"])
        check("commit files", st == 200 and [f["path"] for f in j["files"]] == ["src/app.py"], j)
        check("commit stats", j["additions"] == 1 and j["deletions"] == 1, j)
        check("commit patch text", "print('hello')" in j["files"][0]["patch"], j["files"][0]["patch"][:120])
        st, j, _ = get("/commit/" + commits[-1]["sha"])
        check("root commit shows its files", st == 200 and len(j["files"]) == 3, j.get("files"))
        merge = next(c for c in commits if c["subject"].startswith("merge"))
        st, j, _ = get("/commit/" + merge["sha"])
        check("merge diffs vs first parent", st == 200 and [f["path"] for f in j["files"]] == ["n.txt"], j)

        print("4) download")
        st, b, h = get("/download/HEAD/src/app.py", raw=True)
        check("download bytes", st == 200 and b == b"print('hello')\n", b)
        check("download is an attachment", 'attachment; filename="app.py"' in
              h.get("Content-Disposition", ""), h)
        check("download 404s a missing path", get("/download/HEAD/nope", raw=True)[0] == 404)

        print("5) hostile refs / paths are refused")
        for bad in ("/tree/HEAD?ref=--output%3Dx", "/tree/HEAD?ref=..", "/raw/HEAD/../etc/passwd",
                    "/commit/-x", "/tree/HEAD?ref=HEAD@%7B1%7D", "/log/HEAD?ref=%2Fetc%2Fpasswd"):
            check("refuses %s" % bad, get(bad, raw=True)[0] in (400, 404))

        print("6) /edit authorization")
        url = "https://example.test/git/%s/demo.git/edit" % owner
        check("no header -> 401",
              post("/edit", {"ref": "master", "path": "x", "content": "x"})[0] == 401)
        check("other repo's header -> 401",
              post("/edit", {"ref": "master", "path": "x", "content": "x"},
                   auth=nip98("https://example.test/git/%s/other.git/edit" % owner))[0] == 401)
        check("read-scoped header -> 401",
              post("/edit", {"ref": "master", "path": "x", "content": "x"},
                   auth=nip98("https://example.test/git/%s/demo.git" % owner))[0] == 401)
        check("non-maintainer -> 401",
              post("/edit", {"ref": "master", "path": "x", "content": "x"},
                   auth=nip98(url, secret=os.urandom(32)))[0] == 401)

        print("7) /edit commits")
        st, b = post("/edit", {"ref": "master", "path": "src/app.py",
                               "content": "print('edited')\n", "message": "web: edit app"},
                     auth=nip98(url))
        check("edit accepted", st == 200 and b.get("ok"), (st, b))
        new = b.get("commit")
        check("30618 tags name the new tip",
              any(t[0] == "refs/heads/master" and t[1] == new for t in b.get("state_tags_30618", [])),
              b.get("state_tags_30618"))
        check("content landed", get("/raw/HEAD/src/app.py", raw=True)[1] == b"print('edited')\n")
        top = get("/log/HEAD?limit=1")[1]["commits"][0]
        check("author is the signing npub", top["email"].endswith("@nostr"), top)
        check("message used", top["subject"] == "web: edit app", top)
        st, b = post("/edit", {"ref": "master", "path": "run.sh", "content": "#!/bin/sh\necho b\n"},
                     auth=nip98(url))
        mode = _git("--git-dir", repo, "ls-tree", "master", "--", "run.sh").stdout.split()[0]
        check("exec bit preserved", st == 200 and mode == b"100755", (st, mode))
        st, b = post("/edit", {"ref": "feature/x", "path": "docs/new.md", "content": "# new\n"},
                     auth=nip98(url))
        check("new file on a slashed branch", st == 200 and b.get("ok"), (st, b))
        check("new file visible", [e["name"] for e in
                                   get("/tree/HEAD/docs?ref=feature/x")[1]["entries"]] == ["new.md"])
        st, b = post("/edit", {"ref": "feature/x", "path": "docs/new.md", "delete": True},
                     auth=nip98(url))
        check("delete accepted", st == 200 and b.get("deleted"), (st, b))
        check("deleted file gone", not any(e["name"] == "docs" for e in
                                          get("/tree/HEAD?ref=feature/x")[1]["entries"]))
        check("deleting a missing file 404s",
              post("/edit", {"ref": "master", "path": "nope.txt", "delete": True},
                   auth=nip98(url))[0] == 404)

        print("8) /edit compare-and-swap")
        st, b = post("/edit", {"ref": "master", "path": "src/app.py", "content": "y\n",
                               "base": "0" * 40}, auth=nip98(url))
        check("stale base -> 409", st == 409 and b.get("error") == "stale", (st, b))

        print("9) /edit path validation")
        for bad in ("../evil", ".git/config", "", "a\tb", "a\nb"):
            st, _b = post("/edit", {"ref": "master", "path": bad, "content": "x"}, auth=nip98(url))
            check("refuses path %r" % bad, st == 400, st)

        print("10) chunked push (Transfer-Encoding: chunked)")
        # Hooks would need the relay Postgres; framing is what's under test, so move them aside.
        hooks = os.path.join(repo, "hooks")
        if os.path.isdir(hooks):
            os.rename(hooks, hooks + ".off")
        w("fetch", "-q", "o"); w("reset", "-q", "--hard", "o/master")
        with open(os.path.join(wt, "big.bin"), "wb") as f:
            f.write(os.urandom(4 * 1024 * 1024))     # a pack far bigger than postBuffer below
        w("add", "-A"); w("commit", "-qm", "big binary")
        w("config", "http.postBuffer", "16384")      # forces git onto chunked encoding
        p = w("push", base, "master:refs/heads/chunked", check_rc=False,
              env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        err = (p.stderr or b"").decode("utf-8", "replace")
        check("chunked push accepted", p.returncode == 0 and "Bad request" not in err,
              (p.returncode, err[-400:]))
        landed = _git("--git-dir", repo, "rev-parse", "refs/heads/chunked").stdout.strip()
        want = w("rev-parse", "master").stdout.strip()
        check("chunked push landed the right sha", landed == want, (landed, want))
        check("repo still fsck-clean",
              _git("--git-dir", repo, "fsck", "--no-progress", check_rc=False).returncode == 0)
        clone = tempfile.mkdtemp(prefix="grasp-clone-")
        p = _git("clone", "-q", base, os.path.join(clone, "c"), check_rc=False)
        check("Content-Length clone still works", p.returncode == 0, p.stderr[-300:])
        shutil.rmtree(clone, ignore_errors=True)

        print("11) maintainer-npub alias")
        # ngit derives a clone URL per key in the 30617 `maintainers` tag, so a second maintainer's
        # npub is probed as a path that has no directory on disk.
        msk = os.urandom(32)
        maint = nostr_service.derive_pubkey(msk)
        abase = "http://127.0.0.1:%d/%s/demo.git" % (port, maint)

        def aget(path):
            try:
                r = urllib.request.urlopen(abase + path, timeout=20)
                return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")[:200]

        gh._alias_cache.clear()
        check("no DSN -> cannot confirm the ACL -> 404", aget("/refs")[0] == 404, aget("/refs"))
        _real_maints = gh._Handler._maintainers
        gh._CONFIG["pg_dsn"] = "stub"                       # only gates the lookup; ACL is stubbed
        acl_reads = []
        def _stub_maints(self, o, r, _m=maint):
            acl_reads.append((o, r))                        # each call is one Postgres connection
            return {o, _m}
        gh._Handler._maintainers = _stub_maints
        gh._alias_cache.clear()
        try:
            st, j = aget("/refs")
            check("maintainer URL serves the owner's repo", st == 200 and
                  sorted(b["name"] for b in j["branches"]) == ["chunked", "feature/x", "master"], (st, j))
            clone = tempfile.mkdtemp(prefix="grasp-alias-clone-")
            p = _git("clone", "-q", abase, os.path.join(clone, "c"), check_rc=False)
            check("clone via the maintainer URL works", p.returncode == 0, p.stderr[-300:])
            shutil.rmtree(clone, ignore_errors=True)
            # The alias renames the URL; it must not widen the ACL. Delete stays OWNER-only, so a
            # maintainer signing against their own npub path is still refused.
            req = urllib.request.Request(abase + "/delete", data=b"{}", method="POST")
            req.add_header("Authorization", nip98(abase + "/delete", msk))
            try:
                urllib.request.urlopen(req, timeout=20)
                check("alias does not grant delete", False, "delete succeeded")
            except urllib.error.HTTPError as e:
                check("alias does not grant delete", e.code == 401, e.code)
            check("repo survived", os.path.isdir(repo))
            # This lookup runs BEFORE any auth gate, so its cost must be per-REPO, not per-npub:
            # keyed on the caller's path segment, an anonymous client mints a Postgres connection per
            # made-up npub (and evicts the real entries once the cache bound is hit).
            before = len(acl_reads)
            for _ in range(5):
                probe = "http://127.0.0.1:%d/%s/demo.git/refs" % (port, nostr_service.derive_pubkey(
                    os.urandom(32)))
                try:
                    urllib.request.urlopen(probe, timeout=20)
                except urllib.error.HTTPError:
                    pass
            check("unknown npubs cost no extra ACL read", len(acl_reads) == before,
                  "%d -> %d" % (before, len(acl_reads)))
            # ...and the counter is live, not a dead instrument: a COLD cache does read the ACL.
            gh._alias_cache.clear()
            aget("/refs")
            check("a cold cache does read the ACL", len(acl_reads) > before, len(acl_reads))

            # A pubkey that is NOT a maintainer gets nothing.
            gh._Handler._maintainers = lambda self, o, r: {o}
            gh._alias_cache.clear()
            check("non-maintainer npub still 404s", aget("/refs")[0] == 404)
        finally:
            gh._Handler._maintainers = _real_maints
            gh._CONFIG["pg_dsn"] = ""
            gh._alias_cache.clear()
    finally:
        httpd.shutdown()
        shutil.rmtree(wt, ignore_errors=True)
        shutil.rmtree(_STORE, ignore_errors=True)

    failed = _results.count(False)
    print("\n%d/%d checks passed" % (_results.count(True), len(_results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
