"""
Built-in HTTP proxy that forwards traffic through a SOCKS5 proxy (e.g., Tor).
Replaces Privoxy for torrent traffic routing.
"""

import os
import sys
import time
import asyncio
import ipaddress
import socket
import logging
import subprocess
import threading
from typing import Optional
from urllib.parse import urlparse


def _socks_target(spec, host):
    """Parse a backend spec into (host, port, label). Accepts an int/str port, or 'port:label' where
    label is the Tor exit region (us/ca) — used to make the proxy's logs say which daemon served."""
    s = str(spec)
    port, _, label = s.partition(":")
    return (host, int(port), label or port)

logger = logging.getLogger(__name__)


class HttpToSocksProxy:
    """
    HTTP proxy server that forwards all traffic through a SOCKS5 proxy.

    Flow: Client → HTTP Proxy (this) → SOCKS5 (Tor) → Internet
    """

    _instance: Optional['HttpToSocksProxy'] = None
    _lock = threading.Lock()

    def __init__(
        self,
        listen_host: str = "127.0.0.1",
        listen_port: int = 8118,
        socks_host: str = "127.0.0.1",
        socks_port: int = 9052,
        socks_ports: Optional[list] = None,
        allow_direct: bool = False,
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.socks_host = socks_host
        self.socks_port = socks_port
        # Try Tor first and, if EVERY circuit fails, connect DIRECTLY. Off by default and it must stay
        # that way for the main listener: torrent traffic goes through here, and a silent direct
        # connection there is an IP leak. It exists for the second listener (see `--fallback-port`),
        # whose clients — this node's own SearXNG — would rather search from the node's own address
        # than not search at all.
        self.allow_direct = bool(allow_direct)
        # SOCKS backends to load-balance across (one per local Tor daemon). Defaults to the single
        # `socks_port`. Tor-ONLY: if every backend fails we raise (never fall back to a direct
        # connection) so torrent traffic can't leak the real IP.
        _specs = socks_ports or [socks_port]
        self.socks_targets = [_socks_target(s, socks_host) for s in _specs] or [(socks_host, socks_port, str(socks_port))]
        self._rr = 0   # round-robin cursor across socks_targets
        self._stats = {}        # label -> [ok, fail] since the last health summary
        self._stats_since = 0.0

        self._server: Optional[asyncio.AbstractServer] = None
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @classmethod
    def get_instance(
        cls,
        listen_host: str = "127.0.0.1",
        listen_port: int = 8118,
        socks_host: str = "127.0.0.1",
        socks_port: int = 9052,
        socks_ports: Optional[list] = None,
    ) -> 'HttpToSocksProxy':
        """Get or create singleton instance."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(
                    listen_host=listen_host,
                    listen_port=listen_port,
                    socks_host=socks_host,
                    socks_port=socks_port,
                    socks_ports=socks_ports,
                )
            return cls._instance

    def start(self):
        """Start the proxy server in a background thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()
        logger.info(f"HTTP proxy started on {self.listen_host}:{self.listen_port} → SOCKS5 {self.socks_host}:{self.socks_port}")

    def stop(self):
        """Stop the proxy server."""
        self._running = False
        if self._server and self._loop:
            self._loop.call_soon_threadsafe(self._server.close)
            self._loop.call_soon_threadsafe(self._loop.stop)
        logger.info("HTTP proxy stopped")

    def _run_server(self):
        """Run the async server in a dedicated event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._start_server())
            self._loop.run_forever()
        except Exception as e:
            logger.error(f"Proxy server error: {e}")
        finally:
            self._running = False
            self._loop.close()

    async def _start_server(self):
        """Start the async TCP server."""
        # 512 slots: well above Sharkey's deliverJobConcurrency (~300) with headroom for bursts
        self._semaphore = asyncio.Semaphore(512)
        self._server = await asyncio.start_server(
            self._handle_client,
            self.listen_host,
            self.listen_port,
            reuse_address=True,
            backlog=256,
            limit=65536,
        )
        logger.info(f"HTTP proxy listening on {self.listen_host}:{self.listen_port}")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle incoming HTTP proxy request."""
        client_addr = writer.get_extra_info('peername')

        async with self._semaphore:
            try:
                # Read the HTTP request line
                request_line = await asyncio.wait_for(reader.readline(), timeout=30)
                if not request_line:
                    return

                request_line = request_line.decode('utf-8', errors='ignore').strip()
                parts = request_line.split()

                if len(parts) < 3:
                    writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                    await writer.drain()
                    return

                method, target, _ = parts[0], parts[1], parts[2]

                # Read headers
                headers = []
                while True:
                    line = await asyncio.wait_for(reader.readline(), timeout=30)
                    if line == b'\r\n' or line == b'\n' or not line:
                        break
                    headers.append(line)

                if method == 'CONNECT':
                    await self._handle_connect(reader, writer, target, client_addr)
                else:
                    await self._handle_http(reader, writer, method, target, headers, client_addr)

            except asyncio.TimeoutError:
                logger.debug(f"Client {client_addr} timeout")
            except ConnectionResetError:
                logger.debug(f"Client {client_addr} connection reset")
            except Exception as e:
                logger.error(f"Error handling client {client_addr}: {e}")
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

    async def _handle_connect(self, reader, writer, target, client_addr):
        """Handle CONNECT method (HTTPS tunneling)."""
        if ':' in target:
            host, port_str = target.rsplit(':', 1)
            host = host.strip('[]')  # strip IPv6 brackets e.g. [::1]:443 → ::1
            try:
                port = int(port_str)
            except ValueError:
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await writer.drain()
                return
        else:
            host, port = target, 443

        logger.debug(f"[PROXY] CONNECT request: {host}:{port} from {client_addr}")

        try:
            remote_reader, remote_writer = await self._socks_connect(host, port)
        except Exception as e:
            logger.error(f"[PROXY] CONNECT to {target} failed: {type(e).__name__}: {e}")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            return

        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            logger.debug(f"[PROXY] Tunnel established: {host}:{port}")
            await self._tunnel(reader, writer, remote_reader, remote_writer)
        except Exception as e:
            logger.error(f"[PROXY] CONNECT tunnel to {target} failed: {type(e).__name__}: {e}")
        finally:
            try:
                remote_writer.close()
                await remote_writer.wait_closed()
            except Exception:
                pass

    async def _handle_http(self, reader, writer, method, target, headers, client_addr):
        """Handle regular HTTP request."""
        if not target.startswith('http://'):
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return

        parsed = urlparse(target)
        host = parsed.hostname
        if not host:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return
        port = parsed.port or 80
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query

        try:
            remote_reader, remote_writer = await self._socks_connect(host, port)
        except Exception as e:
            logger.error(f"[PROXY] HTTP connect to {host}:{port} failed: {type(e).__name__}: {e}")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            return

        try:
            # Reconstruct request line and headers; strip hop-by-hop, force close
            request = f"{method} {path} HTTP/1.1\r\n"
            host_header = host if port == 80 else f"{host}:{port}"
            content_length = 0
            host_set = False
            for header in headers:
                header_str = header.decode('utf-8', errors='ignore')
                header_lower = header_str.lower()
                if header_lower.startswith('host:'):
                    request += f"Host: {host_header}\r\n"
                    host_set = True
                elif header_lower.startswith(('proxy-', 'connection:', 'keep-alive:')):
                    continue
                else:
                    request += header_str
                    if header_lower.startswith('content-length:'):
                        try:
                            content_length = int(header_lower.split(':', 1)[1].strip())
                        except ValueError:
                            pass

            if not host_set:
                request += f"Host: {host_header}\r\n"
            request += "Connection: close\r\n\r\n"

            remote_writer.write(request.encode())
            await remote_writer.drain()

            # Forward request body (e.g. HTTP tracker POST announce)
            if content_length > 0:
                remaining = content_length
                while remaining > 0:
                    chunk = await asyncio.wait_for(
                        reader.read(min(65536, remaining)), timeout=60
                    )
                    if not chunk:
                        break
                    remote_writer.write(chunk)
                    await remote_writer.drain()
                    remaining -= len(chunk)

            # Forward response
            while True:
                data = await asyncio.wait_for(remote_reader.read(65536), timeout=60)
                if not data:
                    break
                writer.write(data)
                await writer.drain()

        except Exception as e:
            logger.error(f"[PROXY] HTTP request to {host}:{port} failed: {type(e).__name__}: {e}")
            try:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                remote_writer.close()
                await remote_writer.wait_closed()
            except Exception:
                pass

    async def _socks_connect(self, host: str, port: int) -> tuple:
        """Open a tunnel to host:port, load-balancing across the configured SOCKS backends (one per
        local Tor daemon). Round-robins and, on a backend failure, tries the next. TOR-ONLY: if every
        backend fails it RAISES — it never falls back to a direct connection, so torrent traffic can't
        leak the real IP."""
        targets = self.socks_targets
        n = len(targets)
        start = self._rr % n
        self._rr = (self._rr + 1) % n
        tried = []
        last_err = None
        for k in range(n):
            sh, sp, label = targets[(start + k) % n]
            try:
                rw = await self._socks_connect_one(sh, sp, host, port)
                self._record(label, True)
                if tried:   # we failed over from another circuit — say which one actually worked
                    logger.info(f"[PROXY] {host}:{port} served via Tor[{label}]:{sp} (after {', '.join(tried)} failed)")
                return rw
            except Exception as e:
                last_err = e
                self._record(label, False)
                tried.append(f"Tor[{label}]:{sp}")
                logger.debug(f"[PROXY] Tor[{label}]:{sp} failed for {host}:{port}: {e}")
        # Every Tor circuit failed. On the DIRECT-FALLBACK listener, connect straight out rather than
        # failing the request; anywhere else, raise — named backends so the failure is attributable
        # (the relay then tries direct).
        if self.allow_direct:
            logger.warning(f"[PROXY] {host}:{port} — all Tor backends failed ({', '.join(tried)}: {last_err}); "
                           f"connecting DIRECT (fallback listener)")
            rw = await asyncio.open_connection(host, port)
            self._record("direct", True)
            return rw
        raise Exception(f"all Tor backends failed ({', '.join(tried)}): {last_err}")

    def _record(self, label, ok):
        """Tally per-backend ok/fail and, every ~2 min of traffic, log a one-line health summary so
        you can see at a glance which Tor daemon is working and which is failing."""
        s = self._stats.setdefault(label, [0, 0])
        s[0 if ok else 1] += 1
        now = time.time()
        if not self._stats_since:
            self._stats_since = now
        elif now - self._stats_since >= 120:
            summary = " · ".join(f"Tor[{l}] {v[0]}ok/{v[1]}fail" for l, v in self._stats.items())
            logger.info(f"[PROXY] backend health (last {int(now - self._stats_since)}s): {summary}")
            self._stats = {}
            self._stats_since = now

    async def _socks_connect_one(self, socks_host: str, socks_port: int, host: str, port: int) -> tuple:
        """Connect to host:port through ONE SOCKS5 proxy (Tor). Tor's SOCKS5 handles hostnames
        natively (addr_type=0x03), including .onion — no local DNS resolution occurs."""
        logger.debug(f"[PROXY] SOCKS5 connect to {socks_host}:{socks_port} for {host}:{port}")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(socks_host, socks_port),
            timeout=30,
        )

        try:
            # Greeting: version 5, one auth method, no-auth
            writer.write(b'\x05\x01\x00')
            await asyncio.wait_for(writer.drain(), timeout=10)

            response = await asyncio.wait_for(reader.readexactly(2), timeout=10)
            if response[0] != 0x05 or response[1] != 0x00:
                raise Exception(f"SOCKS5 auth failed: {response.hex()}")

            # Connect request
            try:
                addr = ipaddress.ip_address(host)
                if addr.version == 4:
                    addr_type = 0x01
                    addr_bytes = socket.inet_aton(host)
                else:
                    addr_type = 0x04
                    addr_bytes = addr.packed
            except ValueError:
                addr_type = 0x03
                host_bytes = host.encode('utf-8')
                if len(host_bytes) > 255:
                    raise ValueError(f"Hostname too long for SOCKS5 ({len(host_bytes)} bytes): {host[:64]}")
                addr_bytes = bytes([len(host_bytes)]) + host_bytes

            writer.write(bytes([0x05, 0x01, 0x00, addr_type]) + addr_bytes + port.to_bytes(2, 'big'))
            await asyncio.wait_for(writer.drain(), timeout=10)

            # Response — Tor must build the circuit before replying; allow 45s
            # (SocksTimeout 30 in torrc ensures Tor responds within 30s)
            response = await asyncio.wait_for(reader.readexactly(4), timeout=45)
            if response[0] != 0x05:
                raise Exception(f"Invalid SOCKS5 response version: 0x{response[0]:02x}")
            if response[1] != 0x00:
                error_codes = {
                    0x01: "General failure",
                    0x02: "Connection not allowed",
                    0x03: "Network unreachable",
                    0x04: "Host unreachable",
                    0x05: "Connection refused",
                    0x06: "TTL expired",
                    0x07: "Command not supported",
                    0x08: "Address type not supported",
                }
                raise Exception(f"SOCKS5 connect failed: {error_codes.get(response[1], f'Unknown 0x{response[1]:02x}')}")

            # Consume bound address (required by protocol, value not used)
            bound_type = response[3]
            if bound_type == 0x01:
                await asyncio.wait_for(reader.readexactly(6), timeout=10)
            elif bound_type == 0x03:
                length = (await asyncio.wait_for(reader.readexactly(1), timeout=10))[0]
                await asyncio.wait_for(reader.readexactly(length + 2), timeout=10)
            elif bound_type == 0x04:
                await asyncio.wait_for(reader.readexactly(18), timeout=10)

            return reader, writer

        except Exception:
            writer.close()
            await writer.wait_closed()
            raise

    async def _tunnel(self, client_reader, client_writer, remote_reader, remote_writer):
        """Bidirectional data tunnel."""
        async def forward(src, dst):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except Exception:
                pass

        t1 = asyncio.ensure_future(forward(client_reader, remote_writer))
        t2 = asyncio.ensure_future(forward(remote_reader, client_writer))
        try:
            _, pending = await asyncio.wait(
                [t1, t2], timeout=1800, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        except Exception:
            for task in [t1, t2]:
                task.cancel()
            for task in [t1, t2]:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass


def start_http_proxy(
    listen_host: str = "127.0.0.1",
    listen_port: int = 8118,
    socks_host: str = "127.0.0.1",
    socks_port: int = 9052,
) -> HttpToSocksProxy:
    """Start the HTTP proxy and return the instance."""
    proxy = HttpToSocksProxy.get_instance(
        listen_host=listen_host,
        listen_port=listen_port,
        socks_host=socks_host,
        socks_port=socks_port,
    )
    proxy.start()
    return proxy


def stop_http_proxy():
    """Stop the HTTP proxy if running."""
    if HttpToSocksProxy._instance:
        HttpToSocksProxy._instance.stop()


# --- standalone subprocess (own core) ---------------------------------------
# All bot/social media uploads route through this proxy; in-process its asyncio loop
# competed with the app's event loop and pegged a shared core under concurrent uploads.
# Running it as its OWN process gives it a dedicated core. The module is pure-stdlib, so
# it's launched by file path (no app package import) and stays lightweight.

_proxy_process: Optional[subprocess.Popen] = None


def _wait_proxy_listener(process: subprocess.Popen, host: str, port: int,
                         timeout: float = 10.0) -> None:
    """Do not report a booted proxy until its child owns the configured TCP listener.

    Popen success only proves fork/exec.  Bind failures happen in the child's asyncio loop a moment
    later, which previously left the role process healthy forever with nothing on :8118.  Requiring
    both a live child and a connectable socket makes systemd's Restart=always retry the whole role.
    """
    deadline = time.monotonic() + max(0.1, float(timeout))
    connect_host = "127.0.0.1" if str(host) in ("", "0.0.0.0", "::") else str(host)
    last_error = None
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"HTTP proxy exited before readiness (status {code})")
        try:
            with socket.create_connection((connect_host, int(port)), timeout=0.25):
                if process.poll() is None:
                    return
        except OSError as exc:
            last_error = exc
        time.sleep(0.05)
    raise RuntimeError(f"HTTP proxy did not listen on {connect_host}:{port}: {last_error}")


def start_http_proxy_process(
    listen_host: str = "127.0.0.1",
    listen_port: int = 8118,
    socks_host: str = "127.0.0.1",
    socks_port: int = 9052,
    socks_ports: Optional[list] = None,
    fallback_port: int = 0,
) -> subprocess.Popen:
    """Spawn the proxy as a separate process. Idempotent (reuses a live child). `socks_ports` (one
    port per local Tor daemon) is the load-balance set; defaults to the single `socks_port`.
    `fallback_port` additionally opens the Tor→Tor→DIRECT listener (0 = don't)."""
    global _proxy_process
    if _proxy_process and _proxy_process.poll() is None:
        return _proxy_process
    ports_csv = ",".join(str(p) for p in (socks_ports or [socks_port]))
    argv = [
        sys.executable, os.path.abspath(__file__),
        "--listen-host", str(listen_host), "--listen-port", str(listen_port),
        "--socks-host", str(socks_host), "--socks-ports", ports_csv,
    ]
    if fallback_port:
        argv += ["--fallback-port", str(fallback_port)]
    _proxy_process = subprocess.Popen(argv)
    try:
        _wait_proxy_listener(_proxy_process, listen_host, listen_port)
    except Exception:
        try:
            if _proxy_process.poll() is None:
                _proxy_process.terminate()
                _proxy_process.wait(timeout=2)
        except Exception:
            try: _proxy_process.kill()
            except Exception: pass
        _proxy_process = None
        raise
    logger.info(f"HTTP proxy subprocess started (pid {_proxy_process.pid}) on "
                f"{listen_host}:{listen_port} → SOCKS5 {socks_host}:[{ports_csv}]"
                + (f" (+ direct-fallback listener on {fallback_port})" if fallback_port else ""))
    return _proxy_process


def role_healthy() -> bool:
    """Health contract consumed by role_runner after startup readiness has succeeded."""
    return _proxy_process is not None and _proxy_process.poll() is None


def stop_http_proxy_process():
    """Terminate the proxy subprocess if running."""
    global _proxy_process
    if _proxy_process and _proxy_process.poll() is None:
        _proxy_process.terminate()
        try:
            _proxy_process.wait(timeout=10)
        except Exception:
            _proxy_process.kill()
        logger.info("HTTP proxy subprocess stopped")
    _proxy_process = None


def _run_standalone():
    import argparse
    parser = argparse.ArgumentParser(description="PosterChanAI HTTP→SOCKS5 proxy (standalone)")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=8118)
    parser.add_argument("--socks-host", default="127.0.0.1")
    parser.add_argument("--socks-port", type=int, default=9052)
    parser.add_argument("--socks-ports", default="", help="CSV of SOCKS ports to load-balance across (Tor daemons)")
    parser.add_argument("--fallback-port", type=int, default=0,
                        help="also listen here with Tor→Tor→DIRECT fallback (0 = don't)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [http-proxy] %(message)s")
    _specs = [p.strip() for p in args.socks_ports.split(",") if p.strip()] or [args.socks_port]
    proxy = HttpToSocksProxy.get_instance(
        listen_host=args.listen_host, listen_port=args.listen_port,
        socks_host=args.socks_host, socks_ports=_specs,
    )
    # The second listener, in the SAME process: same Tor backends and round-robin, but it falls back
    # to a direct connection when every circuit is down. A separate PORT and not a flag on the main
    # one, because who may connect directly is the entire distinction — torrents must never reach it.
    fallback = None
    if args.fallback_port and args.fallback_port != args.listen_port:
        fallback = HttpToSocksProxy(
            # LOOPBACK, always — never `args.listen_host`. The main proxy is deliberately bindable to
            # a LAN address (a shared proxy for other nodes), but this one CONNECTS DIRECTLY when Tor
            # is down: on a LAN address that is an open proxy anyone on the network can use to reach
            # the internet from this box. Its only client is this node's own SearXNG, which runs in
            # the host namespace precisely so loopback is enough.
            listen_host="127.0.0.1", listen_port=args.fallback_port,
            socks_host=args.socks_host, socks_ports=_specs, allow_direct=True,
        )

    async def _serve():
        await proxy._start_server()
        if fallback:
            # NEVER fatal. This listener is a convenience for search; the one above carries the
            # node's whole outbound stack, and a busy port (or a bad `proxy_fallback_port`) taking
            # BOTH down would turn "search has no direct fallback" into "nothing has a proxy at all".
            try:
                await fallback._start_server()
                asyncio.create_task(fallback._server.serve_forever())
            except Exception as e:
                logger.warning("[PROXY] direct-fallback listener on %s did not start (%s) — searches "
                               "will use whatever Admin → Tools points at, through the Tor-only proxy",
                               fallback.listen_port, e)
        await proxy._server.serve_forever()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    _run_standalone()


def start_from_settings() -> bool:
    """Start the built-in HTTP proxy from settings. Returns True if it was started.

    EXTRACTED from app/main.py so the `proxy` role runs the identical code path — see
    tor_service.start_from_settings for why this is not duplicated into the role runner.
    """
    from app.services import settings_store as _ss
    # Split roles can reach this point before the relay has an operator/settings document (most
    # visibly during boot).  The canonical first-run default in app.database is ON; using
    # get_bool()'s generic False default here silently inverted that default and left the role
    # process healthy while nothing listened on :8118.  Keep the service-local fallback identical
    # to the canonical/env-controlled default so a temporarily empty cache cannot disable egress.
    _enabled_default = os.environ.get("POSTERCHANAI_PROXY_ENABLED", "true").strip().lower() \
        in ("1", "true", "yes", "on")
    if not _ss.get_bool("proxy_enabled", _enabled_default):
        return False
    socks_host = _ss.get(
        "proxy_socks_host", os.environ.get("POSTERCHANAI_PROXY_SOCKS_HOST", "127.0.0.1")
    )
    if not socks_host:
        logger.warning("[PROXY] enabled but no SOCKS5 target host configured")
        return False
    _pport = _ss.get_int("proxy_listen_port", 8118)
    # Load-balance across both local Tor daemons when the 2nd is on. Tor-only (no direct fallback) —
    # keeps torrent traffic from ever leaking the real IP. Each backend is labelled with its exit
    # region so the proxy log says which daemon served/failed a request. Only LB onto the 2nd port
    # when this deployment actually runs it, else it is a dead port.
    _l1 = ((_ss.get("tor_exit_nodes", "{us}") or "{us}").strip().strip("{}").split(",")[0] or "tor")
    _socks_ports = [f"{_ss.get_int('proxy_socks_port', 9052)}:{_l1}"]
    _tor_default = os.environ.get("POSTERCHANAI_TOR_ENABLED", "true").strip().lower() \
        in ("1", "true", "yes", "on")
    _tor2_default = os.environ.get("POSTERCHANAI_TOR2_ENABLED", "true").strip().lower() \
        in ("1", "true", "yes", "on")
    if _ss.get_bool("tor_enabled", _tor_default) and _ss.get_bool("tor2_enabled", _tor2_default):
        _l2 = ((_ss.get("tor2_exit_nodes", "{ca}") or "{ca}").strip().strip("{}").split(",")[0] or "tor2")
        _socks_ports.append(f"{_ss.get_int('tor2_socks_port', 9062)}:{_l2}")
    # Second listener: Tor1 → Tor2 → DIRECT. This is what the node's own SearXNG points at, so a
    # search rides Tor when Tor is up and still works when it isn't — SearXNG has no fallback of its
    # own, so without this a Tor outage turns every search (AI web lookups, news, the bots, the Web
    # Search screen) into a timeout that reads as "no results". Never used by torrents, which is why
    # it is a separate PORT rather than a flag on the one above.
    _fport = _ss.get_int("proxy_fallback_port", _pport + 1)
    start_http_proxy_process(
        listen_host=_ss.get("proxy_listen_host", "127.0.0.1"), listen_port=_pport,
        socks_host=socks_host, socks_ports=_socks_ports, fallback_port=_fport)
    logger.info("[PROXY] built-in HTTP proxy (subprocess) started on port %s -> SOCKS %s:%s "
                "(direct-fallback listener on %s)", _pport, socks_host, _socks_ports, _fport)
    return True
