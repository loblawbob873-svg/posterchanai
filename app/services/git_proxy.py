"""GRASP git reverse-proxy — thin, dumb pass-through to a remote git-host node.

Mirrors the Blossom/storage proxy (app/services/storage_proxy.py): when a node sets
`git_server_proxy_url`, it does NOT run its own git-host subprocess — instead its git front forwards
the smart-HTTP git requests to the hosting node, exactly like storage blob bytes live on nas and are
served through a storage proxy. ALL authorization, repo storage, the pre-receive/post-receive hooks,
and the Postgres 30617/30618 lookups stay on the HOSTING node. The proxy is deliberately dumb: it
re-implements NO auth — it just forwards the request (including the Authorization/NIP-98 header,
Content-Type, and Git-Protocol) and streams the reply back.

Trust model: `git_server_proxy_url` is admin-set config (same trust as `storage_server_url`); we
require an http/https scheme and otherwise trust it (LAN peer). We forward the client's NIP-98 header
untouched so the hosting node authorizes reads/writes; we do NOT inject any load-balanced auth-bypass
header (git auth is per-request on the host, not server-to-server).

Streaming choice: the request body is read fully and forwarded WITH its Content-Length (the hosting
node's git_host_main reads CONTENT_LENGTH for the CGI env — a chunked body with no length would break
receive-pack). That means a push packfile is buffered on the proxy (bounded by the per-repo size cap,
and this is a LAN hop — same shape as the storage proxy buffering an upload). The RESPONSE (a clone's
packfile can be large) is STREAMED back chunk-by-chunk.
"""

import logging

import httpx
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from starlette.requests import Request

from app.services import settings_store

logger = logging.getLogger(__name__)

# Request headers we forward to the hosting node. Authorization carries the NIP-98 proof the host's
# read-gate/pre-receive validates; Git-Protocol enables protocol v2; Content-Type marks the git
# service payload. Host/Content-Length are recomputed by httpx; hop-by-hop headers are dropped.
_FORWARD_REQ_HEADERS = (
    "authorization", "content-type", "accept", "accept-encoding", "content-encoding",
    "git-protocol", "user-agent",
)
# Response headers we pass back. Content-Type distinguishes the advertisement vs the result; the
# no-cache set matches git's smart-HTTP semantics for info/refs. We stream the body, so Content-Length
# / Transfer-Encoding are handled by StreamingResponse, not copied.
#
# www-authenticate is REQUIRED, not cosmetic: it is the host's 401 challenge for a private repo. A
# client only authenticates against a scheme the server advertises, so without it git-core never runs
# its credential helper — dropping it made every private repo unreadable through a proxy node while
# working fine when hitting the host directly, which is exactly the bug that direct-to-host testing
# hides. Test private-repo auth through the public URL, not against :3053.
_FORWARD_RESP_HEADERS = ("content-type", "cache-control", "expires", "pragma", "content-encoding",
                         "www-authenticate")


def proxy_enabled() -> bool:
    return bool((settings_store.get("git_server_proxy_url", "") or "").strip())


def _base_url() -> str:
    base = (settings_store.get("git_server_proxy_url", "") or "").strip()
    if not base.startswith(("http://", "https://")):
        logger.error("[git-proxy] git_server_proxy_url missing http(s):// — %r", base)
        raise HTTPException(status_code=500,
                            detail="git_server_proxy_url must be an http(s):// URL")
    return base.rstrip("/")


async def proxy_git_request(request: Request, repo_path: str) -> StreamingResponse:
    """Forward a smart-HTTP git request (`<npub>/<id>.git/...`) to the hosting node and stream back
    the reply. `repo_path` is everything after the `/git/` mount (already URL-path form)."""
    base = _base_url()
    # Reject anything that isn't a smart-HTTP git endpoint or a raw-file read (don't become an open
    # proxy). `.git/raw/<ref>/<path>` is the read-gated single-file read the client's repo view uses to
    # render a README without cloning; the hosting node still applies the private-repo read gate.
    if not (repo_path.endswith("/info/refs")
            or repo_path.endswith("/git-upload-pack")
            or repo_path.endswith("/git-receive-pack")
            or ".git/raw/" in repo_path):
        raise HTTPException(status_code=404, detail="not a git smart-HTTP endpoint")

    target = "%s/%s" % (base, repo_path.lstrip("/"))
    qs = request.url.query
    if qs:
        target = target + "?" + qs

    fwd_headers = {}
    for k in _FORWARD_REQ_HEADERS:
        v = request.headers.get(k)
        if v is not None:
            fwd_headers[k] = v

    method = request.method.upper()
    body = await request.body() if method == "POST" else None   # buffer -> preserves Content-Length

    client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0),
                               transport=httpx.AsyncHTTPTransport(retries=2, http2=False))
    try:
        req = client.build_request(method, target, headers=fwd_headers, content=body)
        resp = await client.send(req, stream=True)
    except httpx.TimeoutException:
        await client.aclose()
        logger.error("[git-proxy] timeout to %s", target)
        raise HTTPException(status_code=504, detail="git host timeout")
    except httpx.ConnectError as e:
        await client.aclose()
        logger.error("[git-proxy] cannot reach git host %s: %s", target, e)
        raise HTTPException(status_code=503, detail="cannot reach git host: %s" % e)
    except Exception as e:
        await client.aclose()
        logger.error("[git-proxy] proxy error to %s: %s", target, e, exc_info=True)
        raise HTTPException(status_code=502, detail="git proxy error")

    out_headers = {}
    for k in _FORWARD_RESP_HEADERS:
        v = resp.headers.get(k)
        if v is not None:
            out_headers[k] = v
    # The host sends TWO www-authenticate challenges (Nostr and Basic) and a dict keeps only the
    # first, which would drop exactly the Basic one ngit needs. Re-emit every value as its own raw
    # header (RFC 7235 permits repeating the field) instead of comma-joining them, since a client
    # that mis-parses a combined challenge list would silently fail to authenticate.
    extra_raw = []
    if resp.status_code == 401:
        challenges = resp.headers.get_list("www-authenticate")
        if len(challenges) > 1:
            out_headers.pop("www-authenticate", None)
            extra_raw = [(b"www-authenticate", c.encode("latin-1")) for c in challenges]

    async def _body():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    logger.info("[git-proxy] %s %s -> %d", method, target, resp.status_code)
    out = StreamingResponse(_body(), status_code=resp.status_code,
                            headers=out_headers,
                            media_type=resp.headers.get("content-type"))
    out.raw_headers.extend(extra_raw)
    return out
