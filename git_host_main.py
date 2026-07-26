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
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

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

# Only these three smart-HTTP shapes are served. Anything else -> 404 (no git spawn, no work).
#   GET  /<owner>/<id>.git/info/refs?service=git-upload-pack|git-receive-pack
#   POST /<owner>/<id>.git/git-upload-pack
#   POST /<owner>/<id>.git/git-receive-pack


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

    def _serve(self, method: str):
        parsed = urlparse(self.path)
        info = _parse_repo_path(parsed.path)
        if info is None:
            return self._deny(404, "not found")
        owner_hex, repo_id, rest = info
        if not ghs.repo_exists(owner_hex, repo_id):
            return self._deny(404, "no such repo")        # never auto-create on read/GET
        # RAW single-file read (README + file browsing in the client's repo view):
        #   GET /<owner>/<id>.git/raw/<ref>/<path>  ->  `git show <ref>:<path>`
        # Read-gated exactly like a clone (private repos need NIP-98). Our /git/ is otherwise smart-HTTP
        # (pack protocol) only, so this is the one way the client can render a README/file without cloning.
        if method == "GET" and (rest == "raw" or rest.startswith("raw/")):
            if not self._read_gate_ok(owner_hex, repo_id):
                return self._deny(401, "authentication required (private repo)", auth=True)
            return self._serve_raw(owner_hex, repo_id, rest[4:])
        # TREE listing (Files browser):  GET /<owner>/<id>.git/tree/<ref>[/<subdir>]  ->  `git ls-tree`
        # JSON of the directory's entries. Read-gated like a clone.
        if method == "GET" and (rest == "tree" or rest.startswith("tree/")):
            if not self._read_gate_ok(owner_hex, repo_id):
                return self._deny(401, "authentication required (private repo)", auth=True)
            return self._serve_tree(owner_hex, repo_id, rest[5:] if rest.startswith("tree/") else "")
        service = _wants_service(parsed.path, parsed.query, method)
        if service is None:
            return self._deny(404, "not found")
        # READ GATE: upload-pack (clone/pull) on a private repo needs NIP-98 read auth. receive-pack
        # (push) is authorized inside the pre-receive hook regardless of private/public.
        if service == "git-upload-pack" and not self._read_gate_ok(owner_hex, repo_id):
            return self._deny(401, "authentication required (private repo)", auth=True)
        return self._exec_backend(method, owner_hex, repo_id, rest, parsed.query)

    def _serve_raw(self, owner_hex: str, repo_id: str, refpath: str):
        """Serve one file's bytes from the bare repo via `git show <ref>:<path>`. refpath = '<ref>/<path>'.
        Read-gated by the caller. Args-only subprocess (no shell); output capped; content-type by extension."""
        import re as _re
        import mimetypes as _mt
        refpath = (refpath or "").strip("/")
        ref, _, path = refpath.partition("/")
        if not ref or not path or ".." in path.split("/") or path.startswith("/"):
            return self._deny(400, "bad ref/path")
        if not _re.match(r"^[A-Za-z0-9_./-]{1,120}$", ref) or len(path) > 512:
            return self._deny(400, "bad ref/path")
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
        ctype = _mt.guess_type(path)[0] or "application/octet-stream"
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

    def _serve_tree(self, owner_hex: str, repo_id: str, refpath: str):
        """List a directory with `git ls-tree -l <ref> [<subdir>/]` -> JSON {ref, path, entries:[{name,
        type, size, path}]}. type is 'tree' (dir) or 'blob' (file). Read-gated by the caller."""
        import re as _re
        import json as _json
        refpath = (refpath or "").strip("/")
        ref, _, subdir = refpath.partition("/")
        if not ref:
            ref = "HEAD"
        if not _re.match(r"^[A-Za-z0-9_./-]{1,120}$", ref) or ".." in subdir.split("/") or len(subdir) > 512:
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
        body = _json.dumps({"ref": ref, "path": subdir, "entries": entries}).encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

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
        if clen is not None:
            env["CONTENT_LENGTH"] = clen
        try:
            proc = subprocess.Popen([GIT_HTTP_BACKEND], env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=_REPO_ROOT)
        except FileNotFoundError:
            return self._deny(500, "git-http-backend not found")

        # Feed the request body to the child in a thread so we can read its stdout concurrently
        # (avoids a pipe deadlock on large bidirectional streams).
        def _pump_in():
            try:
                remaining = int(clen) if clen else 0
                while remaining > 0:
                    chunk = self.rfile.read(min(_CHUNK, remaining))
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
