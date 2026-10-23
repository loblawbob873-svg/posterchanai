"""A hermetic little internet for the web reader's tests: public sites, this node's own network, and DNS.

The reader fetches ARBITRARY pages through the server, behind the SSRF guard. Testing that guard with the guard
patched out (or with hostnames that simply do not resolve) tests nothing, so here the guard and the fetch code are
the shipped ones and only two things are replaced, both at the lowest layer the shipped code reaches:

  DNS      socket.getaddrinfo / socket.gethostbyname answer for names under `.reader-web.test` (what each name
           resolves to is per-test: a public address, loopback, several answers, an answer that changes between
           the guard's lookup and the connect). Any OTHER name fails to resolve -- nothing here may reach real DNS.
  dialling anyio's asyncio backend `connect_tcp` (the call httpx/httpcore makes with an IP, after resolution and
           happy-eyeballs). A dial to one of the fake PUBLIC addresses lands on the local `public` server (which
           routes by Host header); a dial to ANY non-public address -- 127/8, 10/8, 169.254/16, ::1, 0.0.0.0,
           100.64/10 ... -- lands on the `internal` server, which stands for this node's own network and records
           every hit. A dial to any other address is refused, so a test can never leave the machine.

"Refused" is then measured, not inferred: the internal server's hit list stays empty and its secret never comes
back in a response.
"""
import ipaddress
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DOMAIN = ".reader-web.test"
# Global unicast addresses that stand for "a public site". Never dialled for real -- see `_dial`.
PUBLIC = ("93.184.216.34", "93.184.216.35", "93.184.216.36", "2606:2800:220:1:248:1893:25c8:1946")
DEAD_PUBLIC = "93.184.216.99"         # a public answer whose port is closed (refuses the connection)
SECRET = "INTERNAL-ONLY-7f3c9e"       # what the node's own network answers; must never come back to a caller

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")


class _Quiet(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, body=b"", ctype="text/html; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class FakeWeb:
    """`routes[(host, path)] = handler(req) -> None`; `dns[name] = [ip, ...]` or a callable(nth_lookup) -> [ip...]."""

    def __init__(self):
        self.routes, self.dns, self.lookups = {}, {}, {}
        self.passthrough, self.loopback_passthrough = set(), False
        self.internal_hits, self.public_hits, self.dials = [], [], []
        self.sent = {}                 # path -> bytes written (the huge / slow pages)
        self.stop = threading.Event()
        web = self

        class Public(_Quiet):
            def do_GET(self):
                host = (self.headers.get("Host") or "").split(":")[0].lower()
                path = self.path
                web.public_hits.append((host, path))
                h = web.routes.get((host, path.split("?")[0])) or web.routes.get(("*", path.split("?")[0]))
                if not h:
                    return self._send(404, "<html><body><h1>Not here</h1></body></html>")
                h(self)

        class Internal(_Quiet):
            def do_GET(self):
                web.internal_hits.append((self.headers.get("Host"), self.path))
                body = ("<html><head><title>%s</title></head><body><main><p>%s %s</p></main></body></html>"
                        % (SECRET, SECRET, ("node-private configuration line. " * 20)))
                self._send(200, body)

        self.public = ThreadingHTTPServer(("127.0.0.1", 0), Public)
        self.internal = ThreadingHTTPServer(("127.0.0.1", 0), Internal)
        for s in (self.public, self.internal):
            s.daemon_threads = True
            threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.internal_port = self.internal.server_address[1]

    # ---- page kinds -------------------------------------------------------------------------------------------
    def page(self, host, path, html, status=200, ctype="text/html; charset=utf-8", headers=None):
        self.routes[(host, path)] = lambda h: h._send(status, html, ctype, headers)

    def redirect(self, host, path, location, status=302):
        self.routes[(host, path)] = lambda h: h._send(status, "", "text/html", {"Location": location})

    def huge(self, host, path, ctype="text/html; charset=utf-8"):
        """An HTML page that never ends (no Content-Length): only a reader-side cap stops it."""
        web = self

        def h(req):
            req.send_response(200)
            req.send_header("Content-Type", ctype)
            req.send_header("Connection", "close")
            req.end_headers()
            chunk = (b"<p>" + b"All work and no play makes the reader a dull boy. " * 40 + b"</p>\n") * 16
            req.wfile.write(b"<html><head><title>Huge</title></head><body><main>")
            n = 0
            try:
                while not web.stop.is_set():
                    req.wfile.write(chunk)
                    n += len(chunk)
                    web.sent[path] = n
            except OSError:
                pass
            req.close_connection = True
        self.routes[(host, path)] = h

    def slow(self, host, path, every=0.5, ctype="text/html; charset=utf-8"):
        """Headers at once, then one byte every `every` seconds, for ever: never idle long enough for a read
        timeout, so only a whole-fetch deadline ends it (the slow-loris page)."""
        web = self

        def h(req):
            req.send_response(200)
            req.send_header("Content-Type", ctype)
            req.send_header("Connection", "close")
            req.end_headers()
            n = 0
            try:
                req.wfile.write(b"<html><body><main>")
                while not web.stop.is_set():
                    req.wfile.write(b"x")
                    req.wfile.flush()
                    n += 1
                    web.sent[path] = n
                    time.sleep(every)
            except OSError:
                pass
            req.close_connection = True
        self.routes[(host, path)] = h

    def silent(self, host, path, seconds=60):
        """Accepts the request and says nothing at all."""
        web = self

        def h(req):
            web.stop.wait(seconds)
            req.close_connection = True
        self.routes[(host, path)] = h

    # ---- DNS + dialling ---------------------------------------------------------------------------------------
    def resolve(self, name):
        name = name.lower().rstrip(".")
        ans = self.dns.get(name)
        if ans is None:
            return None
        n = self.lookups[name] = self.lookups.get(name, 0) + 1
        return list(ans(n) if callable(ans) else ans)

    def install(self, mp):
        """Patch DNS and dialling (pytest MonkeyPatch). Undone with the MonkeyPatch."""
        import anyio._backends._asyncio as aio
        real_gai, web = socket.getaddrinfo, self
        orig_dial = aio.AsyncIOBackend.connect_tcp

        def _is_literal(h):
            try:
                ipaddress.ip_address(h.strip("[]"))
                return True
            except ValueError:
                return False

        def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            h = host.decode() if isinstance(host, bytes) else (host or "")
            if h.lower().rstrip(".").endswith(DOMAIN):
                ips = web.resolve(h)
                if not ips:
                    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
                out = []
                for ip in ips:
                    fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
                    if family not in (0, socket.AF_UNSPEC, fam):
                        continue
                    sa = (ip, int(port or 0), 0, 0) if fam == socket.AF_INET6 else (ip, int(port or 0))
                    out.append((fam, socket.SOCK_STREAM, 6, "", sa))
                if not out:
                    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
                return out
            if _is_literal(h) or h.lower() in ("localhost", ""):
                return real_gai(host, port, family, type, proto, flags)
            try:                         # inet_aton forms (2130706433, 0x7f.1, 127.1) resolve locally, no DNS
                socket.inet_aton(h)
                return real_gai(host, port, family, type, proto, flags | socket.AI_NUMERICHOST)
            except OSError:
                pass
            raise socket.gaierror(socket.EAI_NONAME, "hermetic test: no real DNS for %r" % h)

        def gethostbyname(name):
            h = (name or "").lower().rstrip(".")
            if h.endswith(DOMAIN):
                v4 = [ip for ip in (web.resolve(h) or []) if ":" not in ip]
                if not v4:
                    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
                return v4[0]
            return socket.inet_ntoa(socket.inet_aton(h)) if h not in ("localhost",) else "127.0.0.1"

        async def dial(cls, host, port, local_address=None):
            ip = ipaddress.ip_address(host)
            web.dials.append((str(ip), port))
            if str(ip) == DEAD_PUBLIC:
                raise ConnectionRefusedError("fake public host refuses")
            if str(ip) in PUBLIC:
                return await orig_dial("127.0.0.1", web.public.server_address[1], local_address)
            mapped = getattr(ip, "ipv4_mapped", None)
            if not ip.is_global or (mapped is not None and not mapped.is_global):
                # In a test that runs a whole node in this process, its own loopback services (the searx stub, the
                # relay) are dialled by address and must be reached; the internal server's port still counts.
                if (str(ip), port) in web.passthrough or (
                        web.loopback_passthrough and ip.is_loopback and port != web.internal_port):
                    return await orig_dial(host, port, local_address)
                return await orig_dial("127.0.0.1", web.internal_port, local_address)
            raise OSError("hermetic test: refusing to dial a real address %s" % ip)

        self.passthrough = set()
        mp.setattr(socket, "getaddrinfo", getaddrinfo)
        mp.setattr(socket, "gethostbyname", gethostbyname)
        mp.setattr(aio.AsyncIOBackend, "connect_tcp", classmethod(dial))
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            mp.delenv(k, raising=False)
        mp.setenv("NO_PROXY", "*")

    def close(self):
        self.stop.set()
        for s in (self.public, self.internal):
            s.shutdown()
            s.server_close()
