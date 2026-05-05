"""
Built-in HTTP proxy that forwards traffic through a SOCKS5 proxy (e.g., Tor).
Replaces Privoxy for torrent traffic routing.
"""

import asyncio
import ipaddress
import socket
import logging
import threading
from typing import Optional
from urllib.parse import urlparse

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
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.socks_host = socks_host
        self.socks_port = socks_port

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
    ) -> 'HttpToSocksProxy':
        """Get or create singleton instance."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(
                    listen_host=listen_host,
                    listen_port=listen_port,
                    socks_host=socks_host,
                    socks_port=socks_port,
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
            logger.error(f"[PROXY] CONNECT tunnel to {target} failed: {e}")
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
            content_length = 0
            host_set = False
            for header in headers:
                header_str = header.decode('utf-8', errors='ignore')
                header_lower = header_str.lower()
                if header_lower.startswith('host:'):
                    request += f"Host: {host}\r\n"
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
                request += f"Host: {host}\r\n"
            request += "Connection: close\r\n\r\n"

            remote_writer.write(request.encode())

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
                    remaining -= len(chunk)

            await remote_writer.drain()

            # Forward response
            while True:
                data = await asyncio.wait_for(remote_reader.read(65536), timeout=60)
                if not data:
                    break
                writer.write(data)
                await writer.drain()

        except Exception as e:
            logger.error(f"[PROXY] HTTP request to {host}:{port} failed: {e}")
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
        """Connect to host:port through the SOCKS5 proxy (Tor).

        Tor's SOCKS5 handles hostnames natively (addr_type=0x03), including
        .onion — no local DNS resolution occurs.
        """
        logger.debug(f"[PROXY] SOCKS5 connect to {self.socks_host}:{self.socks_port} for {host}:{port}")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.socks_host, self.socks_port),
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

            # Response — Tor must build the circuit before replying; allow 60s
            response = await asyncio.wait_for(reader.readexactly(4), timeout=60)
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
