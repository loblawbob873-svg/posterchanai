"""The webxdc mini-app sandbox ORIGIN — is it actually deployed?

Mini apps (.xdc games, polls, shared editors) run on `xdc.<instance-host>`, a different ORIGIN from
the client, because an app is code somebody else wrote and must not share the localStorage/IndexedDB
this client keeps the reader's Nostr key and session in. The APP already serves both paths that
origin needs (`/__sandbox__/` and `/sw.js`, host-gated in app/main.py) — what a node has to add is
the front door: a DNS record, a certificate, and one vhost. `./install.sh --webxdc` does all three.

WHY THIS FILE EXISTS. Miss that step and there is no error anywhere. The composer still offers
"🎮 Mini app", posting works, the post renders as a cartridge with a Play button, and pressing Play
opens a window that stays blank forever: the frame is pointed at a hostname that does not resolve,
so nothing is ever requested from this server and nothing is ever logged by it. Every symptom is on
the other side of a boundary neither the operator nor this process can see across.

So this says it ONCE, at startup, in the log. Deliberately not fatal and deliberately not repeated:
a node whose operator does not want mini apps is not broken, and a warning on a timer is a warning
nobody reads. It is also deliberately not a health CHECK anything depends on — it runs in a
background task after boot has finished, every network call is bounded, and every failure inside it
is swallowed. A DNS server that is slow or absent must cost the app nothing.
"""
import asyncio
import logging
import os
import socket
from urllib.parse import urlparse

# Must match SANDBOX_LABEL in static/js/client/webxdc.js and WEBXDC_SANDBOX_LABEL in app/main.py.
# The client derives the sandbox origin itself and there is no setting for it — that is what makes
# the two halves impossible to get out of step, and it is why this is a constant and not config.
SANDBOX_LABEL = "xdc"

_DOCS = "docs/WEBXDC.md"
_FIX = "./install.sh --webxdc"

# Names that are this node talking to itself, or a LAN-only deployment. Mini apps need a real
# https:// origin (a service worker needs a secure context), so a node reachable only as `nas.lan`
# has nothing to be warned about — it has no public hostname to hang a sandbox off in the first
# place, and saying so on every boot would be noise on every development box.
_PRIVATE_SUFFIXES = (".lan", ".local", ".internal", ".localdomain", ".home", ".arpa", ".test", ".invalid")


def _host_of(value: str) -> str:
    """Hostname out of a URL, a bare host, or a host:port. Anything unusable comes back empty."""
    v = (value or "").strip().strip("@")
    if not v:
        return ""
    if "//" not in v:
        v = "//" + v
    try:
        h = (urlparse(v).hostname or "").strip().lower()
    except Exception:
        return ""
    return h


def _is_public_hostname(h: str) -> bool:
    if not h or "." not in h or h.startswith(SANDBOX_LABEL + "."):
        return False
    if h in ("localhost", "example.com"):
        return False
    if h.endswith(_PRIVATE_SUFFIXES):
        return False
    try:                       # a bare IP is a valid origin and can never have an `xdc.` sibling
        socket.inet_aton(h)
        return False
    except OSError:
        pass
    return ":" not in h        # an IPv6 literal, same reason


def instance_host() -> str:
    """This node's public hostname, from whatever already had to be told it.

    There is no `instance_hostname` setting and adding one would be a fifth place to get it wrong —
    these are the fields an operator has ALREADY filled in with the name people load the client
    from. Empty is a perfectly ordinary answer (a LAN box, a fresh install) and means "say nothing".
    """
    candidates = []
    env = (os.environ.get("POSTERCHANAI_DOMAIN") or "").strip()
    if env:
        candidates.append(env)
    try:
        from app.services import settings_store
        for key in ("blossom_public_url", "git_server_public_base",
                    "nostr_dvm_blossom_url", "nostr_relay_nip05_domain"):
            v = (settings_store.get(key, "") or "").strip()
            if v:
                candidates.append(v)
    except Exception:
        pass
    for c in candidates:
        h = _host_of(c)
        if _is_public_hostname(h):
            return h
    return ""


async def _probe(host: str, timeout: float = 8.0):
    """(ok, detail) for https://<host>/__sandbox__/ — never raises, never blocks longer than timeout."""
    try:
        import httpx
    except Exception:
        return None, "httpx unavailable"
    try:
        # trust_env=False on purpose: this node's HTTP_PROXY points at its own Tor listener (torrents
        # share it), and routing a request for our OWN public hostname through Tor would fail in a way
        # that says nothing about whether the sandbox origin is deployed.
        async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=True) as c:
            r = await c.get(f"https://{host}/__sandbox__/")
        return (r.status_code == 200), f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


async def check_sandbox_host(delay: float = 45.0) -> None:
    """One pass, once, after boot. Logs at most one line."""
    try:
        await asyncio.sleep(delay)          # settings hydrate from the relay in their own task
        host = instance_host()
        if not host:
            return
        sandbox = f"{SANDBOX_LABEL}.{host}"

        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(
                loop.getaddrinfo(sandbox, 443, type=socket.SOCK_STREAM), timeout=8.0)
        except Exception:
            logging.warning(
                f"[webxdc] {sandbox} does not resolve, so mini apps (.xdc games/polls) cannot run: "
                f"the client loads them from that hostname, which is a separate ORIGIN so untrusted "
                f"app code cannot reach your Nostr key. Posting one will look fine and Play will show "
                f"a blank window with nothing logged here. Fix: add a DNS record for {sandbox} "
                f"pointing at {host}, then run {_FIX} (or route it to this app in your own reverse "
                f"proxy). See {_DOCS}.")
            return

        ok, detail = await _probe(sandbox)
        if ok:
            logging.info(f"[webxdc] mini-app sandbox origin https://{sandbox} is live")
        elif ok is None:
            return                          # no httpx: DNS resolved, that is as far as we can check
        else:
            logging.warning(
                f"[webxdc] {sandbox} resolves but https://{sandbox}/__sandbox__/ did not answer 200 "
                f"({detail}). Mini apps will open a blank window. It needs a TLS vhost for {sandbox} "
                f"proxying /__sandbox__/ and /sw.js to this app — {_FIX} installs one, or see {_DOCS} "
                f"for the one-line rule if you terminate TLS elsewhere.")
    except Exception as e:                  # a diagnostic must never be able to break a boot
        logging.debug(f"[webxdc] sandbox host check skipped: {e}")


def start_sandbox_host_check(delay: float = 45.0):
    """Fire-and-forget. Returns the task so a caller can await it in a test."""
    return asyncio.create_task(check_sandbox_host(delay))
