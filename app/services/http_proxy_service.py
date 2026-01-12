"""
Built-in HTTP proxy that forwards traffic through a SOCKS5 proxy (e.g., Tor).
Replaces Privoxy for torrent traffic routing.
"""

import asyncio
import socket
import logging
import threading
from typing import Optional

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
        socks_port: int = 9050,
    ):
        self.listen_host = listen_host
        self.listen_port = listen_port
        self.socks_host = socks_host
        self.socks_port = socks_port

        self._server: Optional[asyncio.AbstractServer] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @classmethod
    def get_instance(
        cls,
        listen_host: str = "127.0.0.1",
        listen_port: int = 8118,
        socks_host: str = "127.0.0.1",
        socks_port: int = 9050,
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
            self._loop.close()

    async def _start_server(self):
        """Start the async TCP server."""
        self._server = await asyncio.start_server(
            self._handle_client,
            self.listen_host,
            self.listen_port,
            reuse_address=True,
        )
        logger.info(f"HTTP proxy listening on {self.listen_host}:{self.listen_port}")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle incoming HTTP proxy request."""
        client_addr = writer.get_extra_info('peername')

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
                # HTTPS tunneling (CONNECT method)
                await self._handle_connect(reader, writer, target, client_addr)
            else:
                # Regular HTTP request
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
        # Parse host:port from target
        if ':' in target:
            host, port = target.rsplit(':', 1)
            port = int(port)
        else:
            host, port = target, 443

        try:
            # Connect to target through SOCKS5
            remote_reader, remote_writer = await self._socks5_connect(host, port)

            # Send success response
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()

            # Bidirectional tunnel
            await self._tunnel(reader, writer, remote_reader, remote_writer)

        except Exception as e:
            logger.error(f"CONNECT to {target} failed: {e}")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()

    async def _handle_http(self, reader, writer, method, target, headers, client_addr):
        """Handle regular HTTP request."""
        # Parse URL
        if target.startswith('http://'):
            # Absolute URL
            from urllib.parse import urlparse
            parsed = urlparse(target)
            host = parsed.hostname
            port = parsed.port or 80
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
        else:
            # Should not happen for proxy requests
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return

        try:
            # Connect to target through SOCKS5
            remote_reader, remote_writer = await self._socks5_connect(host, port)

            # Reconstruct and forward the request
            request = f"{method} {path} HTTP/1.1\r\n"

            # Forward headers, update Host
            host_set = False
            for header in headers:
                header_str = header.decode('utf-8', errors='ignore')
                if header_str.lower().startswith('host:'):
                    request += f"Host: {host}\r\n"
                    host_set = True
                elif header_str.lower().startswith('proxy-'):
                    # Skip proxy headers
                    continue
                else:
                    request += header_str

            if not host_set:
                request += f"Host: {host}\r\n"

            request += "\r\n"

            remote_writer.write(request.encode())
            await remote_writer.drain()

            # Forward response
            while True:
                data = await asyncio.wait_for(remote_reader.read(8192), timeout=60)
                if not data:
                    break
                writer.write(data)
                await writer.drain()

            remote_writer.close()
            await remote_writer.wait_closed()

        except Exception as e:
            logger.error(f"HTTP request to {host}:{port} failed: {e}")
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()

    async def _socks5_connect(self, host: str, port: int) -> tuple:
        """Connect to a host through the SOCKS5 proxy."""
        # Connect to SOCKS5 proxy
        reader, writer = await asyncio.open_connection(self.socks_host, self.socks_port)

        try:
            # SOCKS5 greeting
            writer.write(b'\x05\x01\x00')  # Version 5, 1 auth method, no auth
            await writer.drain()

            response = await reader.readexactly(2)
            if response[0] != 0x05 or response[1] != 0x00:
                raise Exception(f"SOCKS5 auth failed: {response.hex()}")

            # SOCKS5 connect request
            # Version, Connect, Reserved, Address type
            if self._is_ip(host):
                # IPv4
                addr_type = 0x01
                addr_bytes = socket.inet_aton(host)
            else:
                # Domain name
                addr_type = 0x03
                host_bytes = host.encode('utf-8')
                addr_bytes = bytes([len(host_bytes)]) + host_bytes

            request = bytes([0x05, 0x01, 0x00, addr_type]) + addr_bytes + port.to_bytes(2, 'big')
            writer.write(request)
            await writer.drain()

            # Read response header
            response = await reader.readexactly(4)
            if response[0] != 0x05:
                raise Exception(f"Invalid SOCKS5 response version")
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
                raise Exception(f"SOCKS5 connect failed: {error_codes.get(response[1], 'Unknown')}")

            # Read bound address (we don't need it, but must consume)
            addr_type = response[3]
            if addr_type == 0x01:  # IPv4
                await reader.readexactly(4 + 2)
            elif addr_type == 0x03:  # Domain
                length = (await reader.readexactly(1))[0]
                await reader.readexactly(length + 2)
            elif addr_type == 0x04:  # IPv6
                await reader.readexactly(16 + 2)

            return reader, writer

        except Exception as e:
            writer.close()
            await writer.wait_closed()
            raise

    def _is_ip(self, host: str) -> bool:
        """Check if host is an IP address."""
        try:
            socket.inet_aton(host)
            return True
        except socket.error:
            return False

    async def _tunnel(self, client_reader, client_writer, remote_reader, remote_writer):
        """Bidirectional data tunnel."""
        async def forward(src, dst):
            try:
                while True:
                    data = await asyncio.wait_for(src.read(8192), timeout=300)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except asyncio.TimeoutError:
                pass
            except Exception:
                pass

        # Run both directions concurrently
        await asyncio.gather(
            forward(client_reader, remote_writer),
            forward(remote_reader, client_writer),
            return_exceptions=True
        )

        try:
            remote_writer.close()
            await remote_writer.wait_closed()
        except Exception:
            pass


def start_http_proxy(
    listen_host: str = "127.0.0.1",
    listen_port: int = 8118,
    socks_host: str = "127.0.0.1",
    socks_port: int = 9050,
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
