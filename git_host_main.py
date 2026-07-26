#!/usr/bin/env python3
"""GRASP git smart-HTTP host — child subprocess (P0 + private read gate).

Spawned + watchdogged by app/services/git_http_service.py (mirrors relay_main.py). Runs in its OWN
OS process so ALL git work — upload-pack/receive-pack/pack generation/gc — happens here, NEVER on
the app's port-3051 event loop. It binds 127.0.0.1:<git_server_port> (default 3053), never public;
the public edge reaches it via the existing nginx (`location /git/` -> proxy_pass 127.0.0.1:3053).

It is CPU-cheap by construction:
  - `git http-backend` is exec'd as CGI, so git streams packs itself (we relay bytes in fixed
    chunks; nothing buffers a whole repo in Python);
  - the only routes accepted are the three smart-HTTP endpoints; everything else is 404 with no work;
  - the push-auth hook (pre-receive) does one indexed Postgres read + one BIP-340 verify;
  - PRIVATE-repo reads do one extra indexed read (maintainer ACL) — public reads hit no DB at all.

Config is read once at startup (via git_http_service._read_config) and mirrored to a sidecar the
hooks read. The per-request NIP-98 header rides through to the hook as GRASP_NIP98 env.
"""

import json
import logging
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from app.services import git_host_service as ghs
from app.services import git_auth

log = logging.getLogger("git-host-proc")


def _resolve_backend() -> str:
    """git-http-backend ships with git but its dir varies by distro. Confirmed here at
    /usr/libexec/git-core; Debian/Ubuntu use /usr/lib/git-core. Env override wins."""
    env = os.environ.get("GRASP_GIT_HTTP_BACKEND")
    if env and os.path.exists(env):
        return env
    for p in ("/usr/libexec/git-core/git-http-backend", "/usr/lib/git-core/git-http-backend"):
        if os.path.exists(p):
            return p
    return "/usr/libexec/git-core/git-http-backend"


GIT_HTTP_BACKEND = _resolve_backend()
_CHUNK = 64 * 1024
_MAX_BODY = 2 * 1024 * 1024 * 1024   # 2 GiB hard ceiling on a single request body (the size cap in
#                                      the hook is the real per-repo bound; this just stops a runaway).

# Only these shapes are served. Anything else -> 404 (no git spawn, no work).
#   GET  /<owner>/<id>.git/info/refs?service=git-upload-pack|git-receive-pack
#   POST /<owner>/<id>.git/git-upload-pack
#   POST /<owner>/<id>.git/git-receive-pack
# Plus the read-only browse API the web UI renders from (all read-gated exactly like a clone):
#   GET  /<owner>/<id>.git/raw/<ref>/<path>           one file's bytes
#   GET  /<owner>/<id>.git/download/<ref>/<path>      same, as an attachment (bigger cap, streamed)
#   GET  /<owner>/<id>.git/tree/<ref>[/<subdir>]      directory listing JSON
#   GET  /<owner>/<id>.git/log/<ref>[/<path>]         commit history JSON
#   GET  /<owner>/<id>.git/refs                       branches + tags JSON
#   GET  /<owner>/<id>.git/commit/<sha>               one commit + its diff JSON
# ...and ONE write route, authorized by a NIP-98 header from a repo MAINTAINER (never a password):
#   POST /<owner>/<id>.git/edit                       commit a single file change (web editor)
# Every browse/write route accepts `?ref=` to carry a ref whose name contains a slash
# (refs/heads/feature/x), which a path segment cannot express.

# A ref must start alphanumeric so it can never be read as a `git` option, and may not contain the
# revision-syntax characters that would turn a browse into a different query (`..`, `@{`, `:`, `^`, `~`).
_REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,119}$")


def _valid_ref(ref: str) -> bool:
    if not ref or not _REF_RE.match(ref):
        return False
    return not (".." in ref or "@{" in ref or ref.endswith(".lock") or "//" in ref)


def _spool_dir() -> str | None:
    """Scratch dir for request spooling + temp indexes: the repo store's own volume, which is the one
    sized for git data (the code/system disk may be small). None -> the system temp dir."""
    try:
        d = os.path.join(ghs.git_project_root(), ".tmp")
        os.makedirs(d, exist_ok=True)
        return d
    except OSError:
        return None


def _npub_or_hex(pubkey_hex: str) -> str:
    try:
        from app.services.nostr import nostr_service
        return nostr_service.npub_of(pubkey_hex) or pubkey_hex
    except Exception:
        return pubkey_hex


def _state_tags(owner_hex: str, repo_id: str, refs: dict) -> list:
    try:
        return ghs.state_tags(owner_hex, repo_id, refs)
    except Exception:
        return []


def _publish_state_witness(owner_hex: str, repo_id: str) -> bool:
    try:
        return ghs.publish_state_witness(owner_hex, repo_id)
    except Exception:
        return False


def _pick_ref(path_ref: str, query: str) -> str:
    """The effective ref: `?ref=` wins over the path segment (a slashed branch name can't live in a
    path segment here, since the segment before the first `/` is all we parse). Falls back to HEAD."""
    q = (parse_qs(query or "").get("ref", [""])[0] or "").strip()
    return q or (path_ref or "").strip() or "HEAD"


def _parse_repo_path(path: str):
    """Split '/<owner>/<id>.git/<service...>' -> (owner_hex, repo_id, service_path) or None.

    Tolerates an nginx prefix (e.g. '/git/'): we scan for the first '<seg>.git' segment and treat the
    segment before it as the owner. Owner is npub or hex; both -> hex. repo_id is strictly sanitized.
    """
    segs = [s for s in path.split("/") if s != ""]
    git_i = None
    for i, s in enumerate(segs):
        if s.endswith(".git"):
            git_i = i
            break
    if git_i is None or git_i == 0:
        return None
    owner_seg = segs[git_i - 1]
    id_seg = segs[git_i][:-4]
    rest = "/".join(segs[git_i + 1:])       # e.g. "info/refs" or "git-upload-pack"
    owner_hex = ghs.owner_hex_from_npub(owner_seg)
    repo_id = ghs.sanitize_repo_id(id_seg)
    if not owner_hex or not repo_id:
        return None
    return owner_hex, repo_id, rest


def _wants_service(path: str, query: str, method: str):
    """Return the git service being requested ('git-upload-pack'|'git-receive-pack') or None."""
    _, _, rest = _parse_repo_path(path) or (None, None, None)
    if rest is None:
        return None
    if method == "GET" and rest == "info/refs":
        svc = parse_qs(query).get("service", [""])[0]
        return svc if svc in ("git-upload-pack", "git-receive-pack") else None
    if method == "POST" and rest in ("git-upload-pack", "git-receive-pack"):
        return rest
    return None


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.info("git-host %s - %s", self.address_string(), fmt % args)

    # ---- helpers -----------------------------------------------------------
    def _deny(self, code: int, msg: str, *, auth: bool = False):
        body = (msg + "\n").encode()
        self.send_response(code)
        if auth:
            # WWW-Authenticate advertises the NIP-98 scheme so a GRASP client knows to sign a header.
            self.send_header("WWW-Authenticate", 'Nostr realm="grasp"')
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _json_out(self, obj, status: int = 200):
        """Send one JSON body. Every browse route answers through here so the framing (length,
        no-cache, broken-pipe tolerance) lives in ONE place instead of being re-typed per route."""
        body = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_gate_ok(self, owner_hex: str, repo_id: str) -> bool:
        """PRIVATE-repo READ authorization (clone/pull). Public repos: always True (fast path, no DB).

        For a private repo require a valid NIP-98 `Authorization: Nostr <b64>` header whose signer is
        in the repo's ACCESS set = maintainers (owner ∪ 30617.maintainers, read from Postgres) ∪ the
        per-repo `readers` list (read from disk). Fail-closed: any error/doubt -> deny. Serves NOTHING
        on denial (the caller 401s before git-http-backend runs, so refs never leak).
        """
        meta = ghs.repo_private_meta(owner_hex, repo_id)
        if not meta.get("private"):
            return True   # public: anonymous clone as before
        header = self.headers.get("Authorization", "")
        if not header:
            return False
        # Build the access allowlist. readers come from disk (cheap); maintainers need one indexed
        # DB read — only for private repos, so public traffic never pays for it.
        allowed = set(meta.get("readers") or [])
        allowed.add(owner_hex)
        dsn = _CONFIG.get("pg_dsn")
        if dsn:
            try:
                import psycopg2
                conn = psycopg2.connect(dsn, connect_timeout=5)
                try:
                    conn.autocommit = True
                    with conn.cursor() as cur:
                        cur.execute("SET statement_timeout = 4000")
                    allowed |= git_auth.load_maintainers(conn, owner_hex, repo_id)
                finally:
                    conn.close()
            except Exception as e:
                log.warning("[git-host] private read ACL DB read failed (%s) -> deny", e)
                return False   # fail-closed: can't confirm ACL -> no read
        # Read gate: bind the header to this repo path; don't require method (a clone reuses one
        # static header across the info/refs GET + upload-pack POST). Wider freshness window than push.
        needle = "%s.git" % repo_id
        signer = git_auth.verify_nip98(header, None, needle, allowed,
                                       max_skew=_CONFIG.get("read_skew", 300), require_method=False)
        if signer:
            log.info("[git-host] private read granted %s -> %s/%s", signer[:12], owner_hex[:12], repo_id)
            return True
        return False

    def _is_wot_member(self, pubkey_hex: str) -> bool:
        """True if pubkey is in the relay's Web of Trust — read from the `wot` table the relay maintains,
        via the SAME Postgres the maintainer ACL uses. Lets any WoT member provision a repo (no explicit
        npub allowlist to curate). Fail-closed on any DB error / no DSN."""
        dsn = _CONFIG.get("pg_dsn")
        if not dsn:
            return False
        try:
            import psycopg2
            conn = psycopg2.connect(dsn, connect_timeout=5)
            try:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute("SET statement_timeout = 4000")
                    cur.execute("SELECT 1 FROM wot WHERE pubkey = %s LIMIT 1", (pubkey_hex,))
                    return cur.fetchone() is not None
            finally:
                conn.close()
        except Exception as e:
            log.warning("[git-host] WoT membership check failed (%s) -> deny", e)
            return False

    def _maintainers(self, owner_hex: str, repo_id: str) -> set:
        """The repo's maintainer ACL = owner ∪ 30617.maintainers, read from the relay Postgres exactly
        as the pre-receive hook reads it (git_auth.load_maintainers re-verifies the announcement's
        signature). Returns just {owner} if there's no DSN — a web commit then needs the URL owner's
        own key, which is the safe reading of "we cannot confirm who else may write"."""
        maints = {owner_hex}
        dsn = _CONFIG.get("pg_dsn")
        if not dsn:
            return maints
        try:
            import psycopg2
            conn = psycopg2.connect(dsn, connect_timeout=5)
            try:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute("SET statement_timeout = 4000")
                maints |= git_auth.load_maintainers(conn, owner_hex, repo_id)
            finally:
                conn.close()
        except Exception as e:
            log.warning("[git-host] maintainer ACL read failed (%s) -> owner only", e)
        return maints

    def _write_gate_signer(self, owner_hex: str, repo_id: str, route: str):
        """WRITE authorization for the web editor. Returns the signing maintainer's hex pubkey, or None.

        Same primitive as a push: a NIP-98 (kind-27235) event, signature re-verified here, bound to
        THIS repo's write route (`<id>.git/<route>` must appear in its `u` tag, so a read-scoped or
        other-repo header can't authorize a commit), method-matched, fresh within the push skew, and
        signed by a key in the maintainer ACL. Fail-closed."""
        allowed = self._maintainers(owner_hex, repo_id)
        return git_auth.verify_nip98(self.headers.get("Authorization", ""), "POST",
                                     "%s.git/%s" % (repo_id, route), allowed,
                                     max_skew=int(_CONFIG.get("write_skew", 120)),
                                     require_method=True)

    def _serve(self, method: str):
        parsed = urlparse(self.path)
        info = _parse_repo_path(parsed.path)
        if info is None:
            return self._deny(404, "not found")
        owner_hex, repo_id, rest = info
        # CREATE is the one POST allowed when the repo does NOT exist yet — a NIP-98-authorized
        # provision (the web "New repo" button). Handled before the repo_exists gate below; it is
        # idempotent (creating an existing repo just returns its clone URL + announce tags).
        if method == "POST" and rest == "create":
            return self._serve_create(owner_hex, repo_id)
        if not ghs.repo_exists(owner_hex, repo_id):
            return self._deny(404, "no such repo")        # never auto-create on read/GET
        # RAW single-file read (README + file browsing in the client's repo view):
        #   GET /<owner>/<id>.git/raw/<ref>/<path>  ->  `git show <ref>:<path>`
        # Read-gated exactly like a clone (private repos need NIP-98). Our /git/ is otherwise smart-HTTP
        # (pack protocol) only, so this is the one way the client can render a README/file without cloning.
        if method == "GET" and (rest == "raw" or rest.startswith("raw/")):
            if not self._read_gate_ok(owner_hex, repo_id):
                return self._deny(401, "authentication required (private repo)", auth=True)
            return self._serve_raw(owner_hex, repo_id, rest[4:], parsed.query)
        # DOWNLOAD: the same bytes as /raw but as an attachment, streamed, with a much bigger cap —
        # /raw is capped at 2 MB because it exists to render a README, and "save this file" is a
        # different job that must not silently hand back a truncated file.
        if method == "GET" and rest.startswith("download/"):
            if not self._read_gate_ok(owner_hex, repo_id):
                return self._deny(401, "authentication required (private repo)", auth=True)
            return self._serve_download(owner_hex, repo_id, rest[len("download/"):], parsed.query)
        # REFS: branches + tags (the branch switcher). Read-gated like a clone.
        if method == "GET" and rest == "refs":
            if not self._read_gate_ok(owner_hex, repo_id):
                return self._deny(401, "authentication required (private repo)", auth=True)
            return self._serve_refs(owner_hex, repo_id)
        # COMMIT: one commit + its diff (what actually changed). Read-gated like a clone.
        if method == "GET" and rest.startswith("commit/"):
            if not self._read_gate_ok(owner_hex, repo_id):
                return self._deny(401, "authentication required (private repo)", auth=True)
            return self._serve_commit(owner_hex, repo_id, rest[len("commit/"):], parsed.query)
        # EDIT (the one write route): commit a single file change, authorized by a maintainer's NIP-98.
        if method == "POST" and rest == "edit":
            return self._serve_edit(owner_hex, repo_id)
        # TREE listing (Files browser):  GET /<owner>/<id>.git/tree/<ref>[/<subdir>]  ->  `git ls-tree`
        # JSON of the directory's entries. Read-gated like a clone.
        if method == "GET" and (rest == "tree" or rest.startswith("tree/")):
            if not self._read_gate_ok(owner_hex, repo_id):
                return self._deny(401, "authentication required (private repo)", auth=True)
            return self._serve_tree(owner_hex, repo_id,
                                    rest[5:] if rest.startswith("tree/") else "", parsed.query)
        # LOG (history):  GET /<owner>/<id>.git/log/<ref>[/<path>]?limit=N  ->  `git log` JSON.
        # Read-gated like a clone.
        if method == "GET" and (rest == "log" or rest.startswith("log/")):
            if not self._read_gate_ok(owner_hex, repo_id):
                return self._deny(401, "authentication required (private repo)", auth=True)
            return self._serve_log(owner_hex, repo_id,
                                   rest[4:] if rest.startswith("log/") else "", parsed.query)
        service = _wants_service(parsed.path, parsed.query, method)
        if service is None:
            return self._deny(404, "not found")
        # READ GATE: upload-pack (clone/pull) on a private repo needs NIP-98 read auth. receive-pack
        # (push) is authorized inside the pre-receive hook regardless of private/public.
        if service == "git-upload-pack" and not self._read_gate_ok(owner_hex, repo_id):
            return self._deny(401, "authentication required (private repo)", auth=True)
        return self._exec_backend(method, owner_hex, repo_id, rest, parsed.query)

    def _split_refpath(self, refpath: str, query: str):
        """'<ref>/<path>' (+ an optional `?ref=` override) -> (ref, path) or None if either is unsafe."""
        refpath = (refpath or "").strip("/")
        path_ref, _, path = refpath.partition("/")
        ref = _pick_ref(path_ref, query)
        path = unquote(path)
        if not path or ".." in path.split("/") or path.startswith("/") or len(path) > 512:
            return None
        if not _valid_ref(ref):
            return None
        return ref, path

    def _serve_raw(self, owner_hex: str, repo_id: str, refpath: str, query: str = ""):
        """Serve one file's bytes from the bare repo via `git show <ref>:<path>`. refpath = '<ref>/<path>'.
        Read-gated by the caller. Args-only subprocess (no shell); output capped; content-type by extension."""
        rp = self._split_refpath(refpath, query)
        if not rp:
            return self._deny(400, "bad ref/path")
        ref, path = rp
        repo_dir = ghs.repo_dir(owner_hex, repo_id)
        if not repo_dir or not os.path.isdir(repo_dir):
            return self._deny(404, "no such repo")
        try:
            proc = subprocess.run(["git", "--git-dir", repo_dir, "show", "%s:%s" % (ref, path)],
                                  capture_output=True, timeout=15)
        except Exception:
            return self._deny(500, "read failed")
        if proc.returncode != 0:
            return self._deny(404, "file not found")
        data = (proc.stdout or b"")[:2 * 1024 * 1024]     # 2 MB cap — a README, not a release tarball
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if path.lower().endswith((".md", ".markdown", ".txt", "")):
            ctype = "text/plain; charset=utf-8"
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # "Download this file" ceiling. Big enough for a real asset, small enough that one request can't
    # be used to stream an arbitrary amount of RAM/bandwidth out of the box.
    _DOWNLOAD_MAX = 64 * 1024 * 1024

    def _serve_download(self, owner_hex: str, repo_id: str, refpath: str, query: str = ""):
        """Stream one file as an attachment (`git cat-file blob <ref>:<path>` piped straight out), so
        the browser saves it instead of rendering it. Streamed in _CHUNK windows — nothing buffers the
        whole blob in Python, matching how the pack routes behave."""
        rp = self._split_refpath(refpath, query)
        if not rp:
            return self._deny(400, "bad ref/path")
        ref, path = rp
        repo_dir = ghs.repo_dir(owner_hex, repo_id)
        if not repo_dir or not os.path.isdir(repo_dir):
            return self._deny(404, "no such repo")
        spec = "%s:%s" % (ref, path)
        # Size first: `cat-file -s` tells us whether the blob exists AND whether it's over the cap
        # before a single byte is read, so an oversized file is refused rather than half-sent.
        try:
            szp = subprocess.run(["git", "--git-dir", repo_dir, "cat-file", "-s", spec],
                                 capture_output=True, timeout=15)
        except Exception:
            return self._deny(500, "read failed")
        if szp.returncode != 0:
            return self._deny(404, "file not found")
        try:
            size = int((szp.stdout or b"0").strip())
        except ValueError:
            return self._deny(500, "read failed")
        if size > self._DOWNLOAD_MAX:
            return self._deny(413, "file is %d bytes — clone the repo to get it" % size)
        name = path.rstrip("/").split("/")[-1] or "file"
        safe = re.sub(r'[^A-Za-z0-9._-]', "_", name)[:100] or "file"
        try:
            proc = subprocess.Popen(["git", "--git-dir", repo_dir, "cat-file", "blob", spec],
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except Exception:
            return self._deny(500, "read failed")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % safe)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            sent = 0
            while sent < size:
                data = proc.stdout.read(min(_CHUNK, size - sent))
                if not data:
                    break
                self.wfile.write(data)
                sent += len(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            proc.wait()

    # Max commits walked to label a directory listing. A listing must stay cheap, and the entries a
    # browser shows are almost always touched recently; anything older just renders without a date
    # rather than making the request crawl through the whole history of a big repo.
    _TREE_LOG_SCAN = 400

    def _last_commits(self, repo_dir: str, ref: str, entries: list, subdir: str) -> dict:
        """Stamp each tree entry with the last commit that touched it (the 'date modified' column
        every git forge shows) and return the tip commit for the listing header.

        ONE `git log --name-only` walk for the whole directory, not `git log -1` per entry — a
        subprocess per file turns a 60-file directory into 60 forks. Entries not touched within
        _TREE_LOG_SCAN commits simply keep commit=None."""
        want = {}
        for e in entries:
            e["commit"] = None
            want[e["path"].rstrip("/")] = e
        head = None
        args = ["git", "--git-dir", repo_dir, "log", "--format=%x01%H%x00%ct%x00%an%x00%s",
                "--name-only", "--max-count=%d" % self._TREE_LOG_SCAN, ref]
        if subdir:
            args += ["--", subdir.rstrip("/") + "/"]
        try:
            proc = subprocess.run(args, capture_output=True, timeout=20)
        except Exception:
            return None
        if proc.returncode != 0:
            return None
        remaining = set(want)
        for block in (proc.stdout or b"").decode("utf-8", "ignore").split("\x01")[1:]:
            meta, _, files = block.partition("\n")
            bits = meta.split("\x00")
            if len(bits) < 4:
                continue
            sha, ts, author, subject = bits[0], bits[1], bits[2], bits[3]
            commit = {"sha": sha, "short": sha[:7], "author": author,
                      "at": int(ts) if ts.isdigit() else 0, "subject": subject}
            if head is None:
                head = commit
            if not remaining:
                continue
            for fp in files.splitlines():
                fp = fp.strip()
                if not fp:
                    continue
                for key in list(remaining):
                    # a file entry matches exactly; a directory matches anything beneath it
                    if fp == key or fp.startswith(key + "/"):
                        want[key]["commit"] = commit
                        remaining.discard(key)
        return head

    def _serve_log(self, owner_hex: str, repo_id: str, refpath: str, query: str = ""):
        """Commit history: GET /<owner>/<id>.git/log/<ref>[/<path>] -> {ref, path, commits:[…]}.
        The repo browser had no history at all — no commit list, and no date on anything — which is
        the first thing anyone looks for in a forge."""
        refpath = (refpath or "").strip("/")
        path_ref, _, path = refpath.partition("/")
        ref = _pick_ref(path_ref, query)
        path = unquote(path)
        if not _valid_ref(ref) or ".." in path.split("/") or len(path) > 512:
            return self._deny(400, "bad ref/path")
        repo_dir = ghs.repo_dir(owner_hex, repo_id)
        if not repo_dir or not os.path.isdir(repo_dir):
            return self._deny(404, "no such repo")
        try:
            limit = int(parse_qs(query or "").get("limit", ["50"])[0])
        except Exception:
            limit = 50
        limit = max(1, min(limit, 200))
        args = ["git", "--git-dir", repo_dir, "log",
                "--format=%H%x00%ct%x00%an%x00%ae%x00%s%x00%b%x02", "--max-count=%d" % limit, ref]
        if path:
            args += ["--", path]
        try:
            proc = subprocess.run(args, capture_output=True, timeout=20)
        except Exception:
            return self._deny(500, "read failed")
        if proc.returncode != 0:
            return self._deny(404, "not found")
        commits = []
        for rec in (proc.stdout or b"").decode("utf-8", "ignore").split("\x02"):
            rec = rec.strip("\n")
            if not rec:
                continue
            bits = rec.split("\x00")
            if len(bits) < 5:
                continue
            commits.append({"sha": bits[0], "short": bits[0][:7],
                            "at": int(bits[1]) if bits[1].isdigit() else 0,
                            "author": bits[2], "email": bits[3], "subject": bits[4],
                            "body": (bits[5].strip() if len(bits) > 5 else "")})
        return self._json_out({"ref": ref, "path": path, "commits": commits})

    # --- branches + tags ------------------------------------------------------------------------
    def _serve_refs(self, owner_hex: str, repo_id: str):
        """GET /<owner>/<id>.git/refs -> {head, default, branches:[…], tags:[…]}.

        Without this the browser could only ever show one ref: every other route takes a ref, but
        nothing told the UI which refs exist. One `for-each-ref` walk covers both lists."""
        repo_dir = ghs.repo_dir(owner_hex, repo_id)
        if not repo_dir or not os.path.isdir(repo_dir):
            return self._deny(404, "no such repo")
        args = ["git", "--git-dir", repo_dir, "for-each-ref",
                "--sort=-committerdate",
                "--format=%(refname)%00%(objectname)%00%(committerdate:unix)%00%(contents:subject)",
                "refs/heads", "refs/tags"]
        try:
            proc = subprocess.run(args, capture_output=True, timeout=20)
        except Exception:
            return self._deny(500, "read failed")
        if proc.returncode != 0:
            return self._deny(500, "read failed")
        branches, tags = [], []
        for line in (proc.stdout or b"").decode("utf-8", "ignore").splitlines():
            bits = line.split("\x00")
            if len(bits) < 2 or not bits[0].startswith("refs/"):
                continue
            full, sha = bits[0], bits[1]
            at = int(bits[2]) if len(bits) > 2 and bits[2].isdigit() else 0
            rec = {"ref": full, "name": full.split("/", 2)[-1], "sha": sha, "short": sha[:7],
                   "at": at, "subject": (bits[3] if len(bits) > 3 else "")}
            (tags if full.startswith("refs/tags/") else branches).append(rec)
        head = ghs.repo_head(owner_hex, repo_id)          # e.g. "refs/heads/master"
        return self._json_out({"head": head, "default": head.split("/", 2)[-1] if head else "",
                               "branches": branches, "tags": tags})

    # --- one commit + its diff -----------------------------------------------------------------
    # A diff has to be bounded: a single generated-file commit can be tens of MB of patch, which no
    # browser wants and no reviewer reads. Over the cap we send what fits and flag it.
    _DIFF_MAX = 700 * 1024
    _DIFF_FILES_MAX = 300

    def _serve_commit(self, owner_hex: str, repo_id: str, sha: str, query: str = ""):
        """GET /<owner>/<id>.git/commit/<sha> -> the commit's metadata, per-file stats and patch.

        `--root` so the very first commit shows its files (it has no parent to diff against), and
        `-m --first-parent` so a merge shows the change it actually brought in rather than nothing."""
        sha = unquote((sha or "").strip("/"))
        if not _valid_ref(sha):
            return self._deny(400, "bad rev")
        repo_dir = ghs.repo_dir(owner_hex, repo_id)
        if not repo_dir or not os.path.isdir(repo_dir):
            return self._deny(404, "no such repo")

        def _git(*args, timeout=25):
            return subprocess.run(["git", "--git-dir", repo_dir, *args], capture_output=True,
                                  timeout=timeout)
        try:
            meta = _git("log", "-1", "--format=%H%x00%ct%x00%an%x00%ae%x00%P%x00%s%x00%b", sha)
        except Exception:
            return self._deny(500, "read failed")
        if meta.returncode != 0:
            return self._deny(404, "no such commit")
        bits = (meta.stdout or b"").decode("utf-8", "ignore").split("\x00")
        if len(bits) < 6:
            return self._deny(404, "no such commit")
        full_sha = bits[0]
        commit = {"sha": full_sha, "short": full_sha[:7],
                  "at": int(bits[1]) if bits[1].isdigit() else 0,
                  "author": bits[2], "email": bits[3],
                  "parents": [p for p in bits[4].split() if p],
                  "subject": bits[5], "body": (bits[6].strip() if len(bits) > 6 else "")}
        # No `-M`: rename detection makes `--numstat` print one combined `dir/{a => b}` field whose
        # text never matches the `diff --git` path, so the stats and the patch would key differently
        # and a rename would render as three rows. Without it a rename is an honest delete + add.
        _dt = ["diff-tree", "-r", "--no-commit-id", "--root", "-m", "--first-parent",
               "--no-color", full_sha]
        files, order = {}, []
        try:
            ns = _git(*_dt, "--numstat")
            if ns.returncode == 0:
                for line in (ns.stdout or b"").decode("utf-8", "ignore").splitlines():
                    parts = line.split("\t")
                    if len(parts) < 3:
                        continue
                    add, dele, p = parts[0], parts[1], parts[-1]
                    if p not in files:
                        order.append(p)
                    files[p] = {"path": p, "patch": "",
                                # "-" means binary, which is not the same as "0 lines changed"
                                "additions": int(add) if add.isdigit() else 0,
                                "deletions": int(dele) if dele.isdigit() else 0,
                                "binary": not (add.isdigit() and dele.isdigit())}
        except Exception:
            pass
        # Per-file patch text, split on the `diff --git` headers of one combined diff-tree run (one
        # subprocess for the whole commit, not one per file).
        truncated = False
        try:
            dp = _git(*_dt, "-p")
            raw = (dp.stdout or b"")
            if len(raw) > self._DIFF_MAX:
                raw, truncated = raw[:self._DIFF_MAX], True
            text = raw.decode("utf-8", "replace")
            for chunk in text.split("\ndiff --git ")[0:]:
                chunk = chunk.strip("\n")
                if not chunk:
                    continue
                if not chunk.startswith("diff --git "):
                    chunk = "diff --git " + chunk
                m = re.match(r'diff --git a/(.+?) b/(.+?)\n', chunk + "\n")
                p = (m.group(2) if m else "").strip()
                if not p:
                    continue
                if p not in files:
                    files[p] = {"path": p, "additions": 0, "deletions": 0, "binary": False}
                    order.append(p)
                files[p]["patch"] = chunk
        except Exception:
            pass
        out = [files[p] for p in order if p in files][:self._DIFF_FILES_MAX]
        if len(order) > self._DIFF_FILES_MAX:
            truncated = True
        commit["files"] = out
        commit["file_count"] = len(order)
        commit["truncated"] = truncated
        commit["additions"] = sum(f["additions"] for f in out)
        commit["deletions"] = sum(f["deletions"] for f in out)
        return self._json_out(commit)

    def _serve_tree(self, owner_hex: str, repo_id: str, refpath: str, query: str = ""):
        """List a directory with `git ls-tree -l <ref> [<subdir>/]` -> JSON {ref, path, entries:[{name,
        type, size, path}]}. type is 'tree' (dir) or 'blob' (file). Read-gated by the caller."""
        refpath = (refpath or "").strip("/")
        path_ref, _, subdir = refpath.partition("/")
        ref = _pick_ref(path_ref, query)
        subdir = unquote(subdir)
        if not _valid_ref(ref) or ".." in subdir.split("/") or len(subdir) > 512:
            return self._deny(400, "bad ref/path")
        repo_dir = ghs.repo_dir(owner_hex, repo_id)
        if not repo_dir or not os.path.isdir(repo_dir):
            return self._deny(404, "no such repo")
        args = ["git", "--git-dir", repo_dir, "ls-tree", "-l", ref]
        if subdir:
            args.append(subdir.rstrip("/") + "/")
        try:
            proc = subprocess.run(args, capture_output=True, timeout=15)
        except Exception:
            return self._deny(500, "read failed")
        if proc.returncode != 0:
            return self._deny(404, "not found")
        entries = []
        for line in (proc.stdout or b"").decode("utf-8", "ignore").splitlines():
            # "<mode> <type> <sha> <size>\t<path>"
            meta, tab, path = line.partition("\t")
            if not tab:
                continue
            parts = meta.split()
            if len(parts) < 4:
                continue
            typ, size = parts[1], parts[3]
            entries.append({"name": path.rstrip("/").split("/")[-1], "type": typ,
                            "size": (int(size) if size.isdigit() else 0), "path": path})
        # dirs first, then files, each alphabetical
        entries.sort(key=lambda e: (e["type"] != "tree", e["name"].lower()))
        head = self._last_commits(repo_dir, ref, entries, subdir)
        return self._json_out({"ref": ref, "path": subdir, "entries": entries, "head": head})

    # --- write: commit one file change (the web editor) -----------------------------------------
    _EDIT_MAX = 2 * 1024 * 1024      # a text editor's file, not an asset upload

    def _serve_create(self, owner_hex: str, repo_id: str):
        """POST /<owner>/<id>.git/create — provision a bare repo, authorized by a NIP-98 header signed
        by the OWNER whose npub is on git_server_allowlist. Body (JSON): {name?, description?, private?}.

        Idempotent (re-creating an existing repo just returns its URLs). Returns the clone URL + the
        suggested NIP-34 30617 tags for the CLIENT to sign+publish — the announcement is the user's own
        event, exactly like the web "Announce a repo" flow, so this endpoint never signs on their behalf."""
        from app.services.nostr import nostr_service
        # WRITE-strength NIP-98: must be signed by the owner (allowed={owner}) and bound to THIS create
        # route, so a header scoped to another repo/route can't be replayed to provision.
        signer = git_auth.verify_nip98(self.headers.get("Authorization", ""), "POST",
                                       "%s.git/create" % repo_id, {owner_hex},
                                       max_skew=int(_CONFIG.get("write_skew", 120)), require_method=True)
        if not signer:
            return self._deny(401, "a NIP-98 signature from the repo owner is required", auth=True)
        # Provisioning gate: ANY web-of-trust member may create a repo — the relay's WoT IS the trust
        # boundary, so we never keep a separate npub allowlist to maintain. An optional
        # git_server_allowlist still grants EXTRA keys (e.g. an operator key that isn't in the social
        # graph). Fail closed only if the owner is in neither.
        allow = set()
        for tok in (_CONFIG.get("allowlist", "") or "").replace(",", "\n").split():
            h = nostr_service.to_pubkey_hex(tok.strip())
            if h:
                allow.add(h)
        if owner_hex not in allow and not self._is_wot_member(owner_hex):
            return self._deny(403, "creating a repo here needs a web-of-trust account on this relay "
                                   "(be followed by the community), or an operator allowlist entry")
        try:
            clen = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._deny(400, "bad content-length")
        body = {}
        if clen > 0:
            if clen > 64 * 1024:
                return self._deny(413, "create body too large")
            try:
                body = json.loads(self.rfile.read(clen).decode("utf-8"))
            except Exception:
                return self._deny(400, "bad json body")
            if not isinstance(body, dict):
                return self._deny(400, "bad json body")
        # IDEMPOTENT + SAFE: if the repo already exists, do NOT re-run create_repo. create_repo
        # re-applies pcai.private / pcai.readers config on EVERY call, so a second "create" (a network
        # retry, or reusing an id) would flip a PRIVATE repo public and WIPE its readers ACL — the web
        # proxy sends no `readers` and defaults `private` to false. Return the existing repo's URLs/tags
        # using its ACTUAL stored private state, changing nothing on disk. (Same wipe class as the
        # replaceable-list / blossom-whitelist bugs: never rebuild an ACL from an empty request.)
        if ghs.repo_exists(owner_hex, repo_id):
            created, private = False, ghs.is_private(owner_hex, repo_id)
        else:
            res = ghs.create_repo(owner_hex, repo_id, private=bool(body.get("private")))
            if not res.get("ok"):
                return self._deny(400, res.get("error", "create failed"))
            created, private = bool(res.get("created")), bool(body.get("private"))

        base = (_CONFIG.get("public_base", "") or "").rstrip("/")
        npub = nostr_service.npub_of(owner_hex) or owner_hex
        clone = "%s/%s/%s.git" % (base, npub, repo_id) if base else ""
        out = {"ok": True, "owner": owner_hex, "npub": npub, "repo_id": repo_id,
               "private": private, "clone": clone, "created": created}
        if not private:
            # Public repos: hand back the 30617 tags for the client to sign. Private repos are never
            # announced (no 30617/30618), matching grasp_selfhost --private + create_repo(private=True).
            tags = [["d", repo_id]]
            if body.get("name"):
                tags.append(["name", str(body["name"])[:200]])
            if body.get("description"):
                tags.append(["description", str(body["description"])[:1000]])
            if clone:
                tags.append(["clone", clone])
            tags.append(["maintainers", owner_hex])
            tags.append(["alt", "git repository: %s" % str(body.get("name") or repo_id)[:80]])
            out["announce_tags_30617"] = tags
        log.info("[git-host] create %s/%s by %s (private=%s)", npub[:12], repo_id, signer[:12], private)
        return self._json_out(out)

    def _serve_edit(self, owner_hex: str, repo_id: str):
        """POST /<owner>/<id>.git/edit — commit a single file add/change/delete, authorized by a
        maintainer's NIP-98 header. Body (JSON):

            {ref, path, content, message?, delete?, base?}

        `base` is the commit sha the editor started from: the update is a compare-and-swap against it
        (409 if the branch moved), so two people editing the same branch can't silently clobber each
        other — the same guarantee a push gets from being a fast-forward.

        The commit is built with plumbing against a TEMPORARY index (never a work tree — this is a
        bare repo), then `update-ref` with the expected old value. No hooks run for this path (it isn't
        receive-pack), so the 30618 witness that post-receive would publish is published here instead."""
        signer = self._write_gate_signer(owner_hex, repo_id, "edit")
        if not signer:
            return self._deny(401, "a repo maintainer's NIP-98 signature is required", auth=True)
        try:
            clen = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._deny(400, "bad content-length")
        if clen <= 0 or clen > self._EDIT_MAX + 64 * 1024:
            return self._deny(413, "edit body too large")
        try:
            body = json.loads(self.rfile.read(clen).decode("utf-8"))
        except Exception:
            return self._deny(400, "bad json body")
        if not isinstance(body, dict):
            return self._deny(400, "bad json body")

        ref = str(body.get("ref") or "HEAD").strip()
        if ref == "HEAD":
            ref = ghs.repo_head(owner_hex, repo_id) or "refs/heads/master"
        if ref.startswith("refs/") and not ref.startswith("refs/heads/"):
            # A tag (or notes/remote ref) is not a branch: committing "onto" it would silently create
            # refs/heads/refs/tags/v1 rather than doing what the caller asked.
            return self._deny(400, "can only commit to a branch")
        if not ref.startswith("refs/heads/"):
            ref = "refs/heads/" + ref.lstrip("/")
        if not _valid_ref(ref[len("refs/heads/"):]) or not _valid_ref(ref):
            return self._deny(400, "bad ref")
        path = str(body.get("path") or "").strip().strip("/")
        # No control characters: `update-index --index-info` is a TAB/newline-delimited format, so a
        # path containing either would let the body inject a second index entry.
        if (not path or len(path) > 512 or ".." in path.split("/")
                or any(ord(c) < 0x20 for c in path)
                or path.startswith(".git/") or path == ".git"):
            return self._deny(400, "bad path")
        delete = bool(body.get("delete"))
        content = body.get("content")
        if not delete:
            if not isinstance(content, str):
                return self._deny(400, "content must be a string")
            data = content.encode("utf-8")
            if len(data) > self._EDIT_MAX:
                return self._deny(413, "file too large to edit here")
        message = (str(body.get("message") or "").strip()
                   or ("delete %s" % path if delete else "update %s" % path))[:2000]
        base = str(body.get("base") or "").strip()

        repo_dir = ghs.repo_dir(owner_hex, repo_id)
        if not repo_dir or not os.path.isdir(repo_dir):
            return self._deny(404, "no such repo")

        def _git(*args, stdin=None, env_extra=None, timeout=30):
            env = dict(os.environ)
            env.update(env_extra or {})
            return subprocess.run(["git", "--git-dir", repo_dir, *args], input=stdin,
                                  capture_output=True, timeout=timeout, env=env)

        # Current tip. A branch that doesn't exist yet is a legal starting point (first commit).
        cur = _git("rev-parse", "--verify", "--quiet", ref + "^{commit}")
        old_sha = (cur.stdout or b"").decode().strip() if cur.returncode == 0 else ""
        if base and old_sha and base != old_sha:
            return self._json_out({"ok": False, "error": "stale", "head": old_sha,
                                   "detail": "the branch moved since you opened this file"}, status=409)
        if delete and not old_sha:
            return self._deny(404, "nothing to delete — branch has no commits")

        idx = None
        try:
            fd, idx = tempfile.mkstemp(prefix="grasp-idx-", dir=_spool_dir())
            os.close(fd)
            os.unlink(idx)                      # git wants to CREATE the index file itself
            genv = {"GIT_INDEX_FILE": idx}
            if old_sha:
                r = _git("read-tree", old_sha, env_extra=genv)
                if r.returncode != 0:
                    return self._deny(500, "read-tree failed")
            # Staging goes through `update-index --index-info` for BOTH add and delete: mode 0 with the
            # null sha is a removal, and unlike `--force-remove` / `--cacheinfo` it needs no work tree,
            # which a bare repo does not have ("fatal: this operation must be run in a work tree").
            if delete:
                if not old_sha:
                    return self._deny(404, "nothing to delete")
                ls = _git("ls-tree", "-z", old_sha, "--", path)
                if not (ls.stdout or b"").strip():
                    return self._deny(404, "no such file on %s" % ref)
                spec = "0 %s\t%s\n" % ("0" * 40, path)
            else:
                blob = _git("hash-object", "-w", "--stdin", stdin=data)
                if blob.returncode != 0:
                    return self._deny(500, "could not store the file")
                bsha = (blob.stdout or b"").decode().strip()
                # Keep the file's existing mode (an executable script must stay executable).
                mode = "100644"
                if old_sha:
                    ls = _git("ls-tree", "-z", old_sha, "--", path)
                    first = (ls.stdout or b"").decode("utf-8", "ignore").split("\x00")[0]
                    if first[:6] in ("100755", "120000"):
                        mode = first[:6]
                if mode == "120000":
                    return self._deny(400, "refusing to edit a symlink")
                spec = "%s %s\t%s\n" % (mode, bsha, path)
            r = _git("update-index", "--index-info", stdin=spec.encode("utf-8"), env_extra=genv)
            if r.returncode != 0:
                return self._deny(400, "could not stage %s" % path)
            tr = _git("write-tree", env_extra=genv)
            if tr.returncode != 0:
                return self._deny(500, "write-tree failed")
            tree = (tr.stdout or b"").decode().strip()
            if old_sha:
                same = _git("rev-parse", "--verify", "--quiet", old_sha + "^{tree}")
                if (same.stdout or b"").decode().strip() == tree:
                    return self._json_out({"ok": True, "unchanged": True, "commit": old_sha,
                                           "ref": ref})
            # The author IS the signing Nostr key — that's the whole identity here, so record it as
            # such (`<npub>@nostr`) rather than inventing a name the signature doesn't back.
            npub = _npub_or_hex(signer)
            ident = {"GIT_AUTHOR_NAME": npub[:32], "GIT_AUTHOR_EMAIL": "%s@nostr" % npub,
                     "GIT_COMMITTER_NAME": npub[:32], "GIT_COMMITTER_EMAIL": "%s@nostr" % npub}
            ct = ["commit-tree", tree]
            if old_sha:
                ct += ["-p", old_sha]
            cm = _git(*ct, stdin=message.encode("utf-8"), env_extra=ident)
            if cm.returncode != 0:
                return self._deny(500, "commit-tree failed")
            new_sha = (cm.stdout or b"").decode().strip()
            # CAS at the git level too: update-ref with the expected old value, so a push landing
            # between our read and our write loses this race instead of being overwritten.
            ur = _git("update-ref", ref, new_sha, old_sha or "")
            if ur.returncode != 0:
                return self._json_out({"ok": False, "error": "stale",
                                       "detail": "the branch moved — reload and re-apply your edit"},
                                      status=409)
        except Exception as e:
            log.warning("[git-host] web edit failed for %s/%s: %s", owner_hex[:12], repo_id, e)
            return self._deny(500, "edit failed")
        finally:
            if idx:
                for p in (idx, idx + ".lock"):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
        log.info("[git-host] web commit %s -> %s/%s %s by %s", new_sha[:12], owner_hex[:12], repo_id,
                 ref, signer[:12])
        refs = ghs.repo_refs(owner_hex, repo_id)
        _publish_state_witness(owner_hex, repo_id)
        return self._json_out({"ok": True, "commit": new_sha, "short": new_sha[:7], "ref": ref,
                               "previous": old_sha, "path": path, "deleted": delete,
                               # The maintainer's OWN 30618 stays the push-authorization authority
                               # (git_auth.decide_push_ref), so hand the client the exact tags to sign
                               # and publish — a web commit ends up as Nostr-attested as a push does.
                               "state_tags_30618": _state_tags(owner_hex, repo_id, refs)})

    # A chunk-size line is a few hex digits; anything longer is a client we don't want to humour.
    _CHUNK_LINE_MAX = 1024

    def _read_chunked_body(self, out, max_bytes: int):
        """Read an RFC-7230 chunked request body from self.rfile into `out`, returning the decoded
        byte count — or None on malformed framing / a body over `max_bytes`.

        Consuming the body EXACTLY (terminating chunk + trailers included) is what keeps the
        connection parseable afterwards; leaving a byte behind is how the old code turned a large
        push into `400 Bad request syntax`."""
        total = 0
        while True:
            line = self.rfile.readline(self._CHUNK_LINE_MAX + 1)
            if not line or len(line) > self._CHUNK_LINE_MAX:
                return None
            line = line.strip()
            if not line:
                continue                       # tolerate a stray blank line between chunks
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)   # drop any chunk extension
            except ValueError:
                return None
            if size < 0:
                return None
            if size == 0:
                # Trailer section: header lines until a blank one (often just the blank line).
                while True:
                    t = self.rfile.readline(self._CHUNK_LINE_MAX + 1)
                    if not t or len(t) > self._CHUNK_LINE_MAX or t in (b"\r\n", b"\n"):
                        break
                return total
            total += size
            if total > max_bytes:
                return None
            remaining = size
            while remaining > 0:
                data = self.rfile.read(min(_CHUNK, remaining))
                if not data:
                    return None
                out.write(data)
                remaining -= len(data)
            if self.rfile.read(2) != b"\r\n":   # every chunk is CRLF-terminated
                return None

    def _exec_backend(self, method, owner_hex, repo_id, rest, query):
        """Exec git-http-backend as CGI and stream stdin->child and child-stdout->client."""
        path_info = "/%s/%s.git/%s" % (owner_hex, repo_id, rest)
        _root = ghs.git_project_root()
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "GIT_PROJECT_ROOT": _root,
            # Propagate the store root so the hooks (fresh subprocesses that re-import git_host_service)
            # resolve the same root for their path-confinement check — the hook reads GRASP_GIT_PROJECT_ROOT
            # from this env, so it never has to re-derive it from settings.
            "GRASP_GIT_PROJECT_ROOT": _root,
            "GIT_HTTP_EXPORT_ALL": "1",           # provisioning is gated by us; every hosted repo exports
            "PATH_INFO": path_info,
            "REQUEST_METHOD": method,
            "QUERY_STRING": query or "",
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "REMOTE_ADDR": self.client_address[0],
            "GIT_PROTOCOL": self.headers.get("Git-Protocol", ""),
            # Static hook config (DSN, caps, flags) so a bare pre-receive process can run fail-closed.
            "GRASP_PG_DSN": _CONFIG.get("pg_dsn", ""),
            "GRASP_REPO_ROOT": _REPO_ROOT,
            "GRASP_REPO_MAX_MB": str(_CONFIG.get("repo_max_mb", 512)),
            "GRASP_ALLOW_FORCE": "1" if _CONFIG.get("allow_force", True) else "0",
            "GRASP_NIP98_ENABLED": "1" if _CONFIG.get("nip98_push", True) else "0",
            "GRASP_PUBLIC_BASE": _CONFIG.get("public_base", ""),
            # The per-request NIP-98 header rides through to the push hook (the admin/sync.sh path).
            "GRASP_NIP98": self.headers.get("Authorization", ""),
        }
        clen = self.headers.get("Content-Length")
        # CHUNKED request bodies. Git switches to `Transfer-Encoding: chunked` as soon as a pack
        # exceeds http.postBuffer (1 MB by default), so this is the NORMAL shape of a first full push.
        # We used to read Content-Length only: the body was never consumed, the leftover chunk framing
        # was then parsed as the next request line, and the push died as `400 Bad request syntax`.
        # De-frame it into a spool file on the repo volume and hand the child a real CONTENT_LENGTH —
        # git-http-backend's CGI contract wants a length, and receive-pack needs the whole pack anyway.
        spool = None
        if clen is None and "chunked" in (self.headers.get("Transfer-Encoding", "") or "").lower():
            try:
                spool = tempfile.TemporaryFile(dir=_spool_dir())
            except OSError:
                return self._deny(500, "no spool space for a chunked body")
            total = self._read_chunked_body(spool, _MAX_BODY)
            if total is None:
                spool.close()
                return self._deny(400, "malformed chunked request body")
            spool.seek(0)
            clen = str(total)
            log.info("[git-host] de-chunked %d byte body for %s", total, path_info)
        if clen is not None:
            env["CONTENT_LENGTH"] = clen
        try:
            proc = subprocess.Popen([GIT_HTTP_BACKEND], env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=_REPO_ROOT)
        except FileNotFoundError:
            if spool:
                spool.close()
            return self._deny(500, "git-http-backend not found")

        # Feed the request body to the child in a thread so we can read its stdout concurrently
        # (avoids a pipe deadlock on large bidirectional streams).
        def _pump_in():
            src = spool or self.rfile
            try:
                remaining = int(clen) if clen else 0
                while remaining > 0:
                    chunk = src.read(min(_CHUNK, remaining))
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
                    remaining -= len(chunk)
            except OSError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                if spool:
                    try:
                        spool.close()
                    except OSError:
                        pass

        t = threading.Thread(target=_pump_in, daemon=True)
        t.start()

        # Parse the CGI header block from the child's stdout, then stream the body through.
        header_buf = b""
        try:
            while b"\r\n\r\n" not in header_buf and b"\n\n" not in header_buf:
                b = proc.stdout.read(1)
                if not b:
                    break
                header_buf += b
                if len(header_buf) > 64 * 1024:
                    break
        except OSError:
            header_buf = b""
        sep = b"\r\n\r\n" if b"\r\n\r\n" in header_buf else b"\n\n"
        head, _, leftover = header_buf.partition(sep)
        status = 200
        headers = []
        for line in head.replace(b"\r\n", b"\n").split(b"\n"):
            if not line.strip():
                continue
            try:
                k, v = line.decode("latin-1").split(":", 1)
            except ValueError:
                continue
            k, v = k.strip(), v.strip()
            if k.lower() == "status":
                try:
                    status = int(v.split()[0])
                except (ValueError, IndexError):
                    status = 200
            else:
                headers.append((k, v))
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        # git streams a chunked body; use chunked transfer so we don't need Content-Length.
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def _wr_chunk(data: bytes):
            self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")

        try:
            if leftover:
                _wr_chunk(leftover)
            while True:
                data = proc.stdout.read(_CHUNK)
                if not data:
                    break
                _wr_chunk(data)
            self.wfile.write(b"0\r\n\r\n")
        except OSError:
            pass
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass
            err = b""
            try:
                err = proc.stderr.read() or b""
            except OSError:
                pass
            proc.wait()
            if proc.returncode not in (0, None) and err:
                log.warning("[git-host] backend rc=%s: %s", proc.returncode, err.decode("latin-1")[:400])

    def do_GET(self):
        self._serve("GET")

    def do_POST(self):
        clen = self.headers.get("Content-Length")
        if clen and int(clen) > _MAX_BODY:
            return self._deny(413, "request too large")
        self._serve("POST")


_CONFIG: dict = {}


def _write_status(running: bool):
    try:
        os.makedirs(os.path.join(_REPO_ROOT, "data"), exist_ok=True)
        p = os.path.join(_REPO_ROOT, "data", "git_http.status.json")
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"running": running, "pid": os.getpid(),
                       "port": _CONFIG.get("port"), "ts": int(__import__("time").time())}, f)
        os.replace(tmp, p)
    except OSError:
        pass


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")
    global _CONFIG
    from app.services.git_http_service import _read_config
    _CONFIG = _read_config()
    if not _CONFIG.get("enabled"):
        log.info("[git-host] disabled (git_server_enabled off) — exiting")
        return
    if not os.path.exists(GIT_HTTP_BACKEND):
        log.error("[git-host] %s missing — install git; exiting", GIT_HTTP_BACKEND)
        return
    _root = ghs.git_project_root()
    os.makedirs(_root, exist_ok=True)
    bind, port = _CONFIG.get("bind", "127.0.0.1"), int(_CONFIG.get("port", 3053))

    class _Server(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = _Server((bind, port), _Handler)
    _write_status(True)
    log.info("[git-host] serving smart-HTTP on http://%s:%d (repos: %s)", bind, port, _root)
    try:
        httpd.serve_forever(poll_interval=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        _write_status(False)


if __name__ == "__main__":
    main()
