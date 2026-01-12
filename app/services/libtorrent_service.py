"""
Built-in torrent client using libtorrent with HTTP proxy support and SCGI server for Flood.
"""

import libtorrent as lt
import asyncio
import threading
import socket
import struct
import xmlrpc.client
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Callable
import logging
import time

logger = logging.getLogger(__name__)


@dataclass
class TorrentInfo:
    """Information about a torrent."""
    info_hash: str
    name: str
    size: int
    downloaded: int
    uploaded: int
    progress: float
    download_rate: int
    upload_rate: int
    state: str
    seeders: int
    peers: int
    eta: int  # seconds, -1 if unknown
    save_path: str
    is_finished: bool
    is_paused: bool


class LibtorrentService:
    """
    All-in-one torrent client using libtorrent.
    - HTTP proxy support for Tor
    - SCGI server for Flood compatibility
    """

    _instance: Optional['LibtorrentService'] = None
    _lock = threading.Lock()

    def __init__(
        self,
        download_dir: str = "/tmp/torrents",
        proxy_host: str = "",
        proxy_port: int = 8118,
        scgi_host: str = "0.0.0.0",
        scgi_port: int = 5001,
    ):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # Create libtorrent session
        self.session = lt.session()

        # Configure session settings - equivalent to rtorrent config:
        # system.daemon.set = true (we run as part of the app)
        # trackers.use_udp.set = yes
        # dht.mode.set = auto
        # protocol.pex.set = yes
        settings = {
            'alert_mask': lt.alert.category_t.all_categories,
            'listen_interfaces': '0.0.0.0:6881',
            'download_rate_limit': 0,  # unlimited
            'upload_rate_limit': 0,
            # Enable DHT (dht.mode.set = auto)
            'enable_dht': True,
            'dht_bootstrap_nodes': 'router.bittorrent.com:6881,router.utorrent.com:6881,dht.transmissionbt.com:6881',
            # Enable PEX (protocol.pex.set = yes)
            'enable_lsd': True,  # Local Service Discovery
            # Enable UDP trackers (trackers.use_udp.set = yes)
            'announce_to_all_trackers': True,
            'announce_to_all_tiers': True,
            # Performance settings
            'connections_limit': 200,
            'active_downloads': 8,
            'active_seeds': 5,
            'active_limit': 15,
        }

        # Configure HTTP proxy if provided (REQUIRED for this app)
        if proxy_host:
            settings.update({
                'proxy_type': lt.proxy_type_t.http,
                'proxy_hostname': proxy_host,
                'proxy_port': proxy_port,
                'proxy_peer_connections': True,
                'proxy_tracker_connections': True,
                'proxy_hostnames': True,
                'force_proxy': True,  # Force ALL traffic through proxy
            })
            logger.info(f"Configured HTTP proxy: {proxy_host}:{proxy_port}")
        else:
            logger.warning("No proxy configured - torrenting disabled for safety")

        self.session.apply_settings(settings)

        # Track torrents by hash
        self.torrents: dict[str, lt.torrent_handle] = {}
        self._number_to_hash: dict[int, str] = {}  # For user-friendly numbering

        # SCGI server
        self.scgi_host = scgi_host
        self.scgi_port = scgi_port
        self._scgi_thread: Optional[threading.Thread] = None
        self._scgi_running = False

        # Alert processing
        self._alert_thread: Optional[threading.Thread] = None
        self._running = False

    @classmethod
    def get_instance(
        cls,
        download_dir: str = "/tmp/torrents",
        proxy_host: str = "",
        proxy_port: int = 8118,
        scgi_host: str = "0.0.0.0",
        scgi_port: int = 5001,
    ) -> 'LibtorrentService':
        """Get or create singleton instance."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(
                    download_dir=download_dir,
                    proxy_host=proxy_host,
                    proxy_port=proxy_port,
                    scgi_host=scgi_host,
                    scgi_port=scgi_port,
                )
                cls._instance.start()
            return cls._instance

    def start(self):
        """Start background threads."""
        if self._running:
            return

        self._running = True

        # Start alert processing thread
        self._alert_thread = threading.Thread(target=self._process_alerts, daemon=True)
        self._alert_thread.start()

        # Start SCGI server
        self._scgi_running = True
        self._scgi_thread = threading.Thread(target=self._run_scgi_server, daemon=True)
        self._scgi_thread.start()

        logger.info(f"LibtorrentService started - SCGI on {self.scgi_host}:{self.scgi_port}")

    def stop(self):
        """Stop background threads."""
        self._running = False
        self._scgi_running = False

        # Save session state
        self.session.pause()

        logger.info("LibtorrentService stopped")

    def _process_alerts(self):
        """Process libtorrent alerts in background."""
        while self._running:
            alerts = self.session.pop_alerts()
            for alert in alerts:
                if isinstance(alert, lt.torrent_finished_alert):
                    logger.info(f"Torrent finished: {alert.torrent_name}")
                elif isinstance(alert, lt.torrent_error_alert):
                    logger.error(f"Torrent error: {alert.torrent_name} - {alert.error}")
            time.sleep(0.5)

    def add_magnet(self, magnet: str) -> str:
        """Add a magnet link. Returns info_hash."""
        params = lt.parse_magnet_uri(magnet)
        params.save_path = str(self.download_dir)

        handle = self.session.add_torrent(params)
        info_hash = str(handle.info_hash())

        self.torrents[info_hash] = handle
        self._update_numbering()

        logger.info(f"Added magnet: {info_hash}")
        return info_hash

    def add_torrent_file(self, torrent_data: bytes) -> str:
        """Add a .torrent file. Returns info_hash."""
        info = lt.torrent_info(lt.bdecode(torrent_data))

        params = lt.add_torrent_params()
        params.ti = info
        params.save_path = str(self.download_dir)

        handle = self.session.add_torrent(params)
        info_hash = str(handle.info_hash())

        self.torrents[info_hash] = handle
        self._update_numbering()

        logger.info(f"Added torrent: {info_hash}")
        return info_hash

    def _update_numbering(self):
        """Update number-to-hash mapping."""
        self._number_to_hash = {
            i + 1: h for i, h in enumerate(self.torrents.keys())
        }

    def get_hash_by_number(self, num: int) -> Optional[str]:
        """Get info_hash by user-friendly number."""
        return self._number_to_hash.get(num)

    def list_torrents(self) -> list[TorrentInfo]:
        """Get list of all torrents."""
        result = []
        for info_hash, handle in self.torrents.items():
            try:
                status = handle.status()
                info = handle.torrent_file()

                # Calculate ETA
                eta = -1
                if status.download_rate > 0:
                    remaining = status.total_wanted - status.total_wanted_done
                    eta = int(remaining / status.download_rate)

                result.append(TorrentInfo(
                    info_hash=info_hash,
                    name=status.name or "Unknown",
                    size=status.total_wanted,
                    downloaded=status.total_wanted_done,
                    uploaded=status.total_upload,
                    progress=status.progress * 100,
                    download_rate=status.download_rate,
                    upload_rate=status.upload_rate,
                    state=self._state_str(status.state),
                    seeders=status.num_seeds,
                    peers=status.num_peers,
                    eta=eta,
                    save_path=status.save_path,
                    is_finished=status.is_finished,
                    is_paused=status.paused,
                ))
            except Exception as e:
                logger.error(f"Error getting status for {info_hash}: {e}")

        return result

    def _state_str(self, state) -> str:
        """Convert state enum to string."""
        states = {
            lt.torrent_status.checking_files: "checking",
            lt.torrent_status.downloading_metadata: "metadata",
            lt.torrent_status.downloading: "downloading",
            lt.torrent_status.finished: "finished",
            lt.torrent_status.seeding: "seeding",
            lt.torrent_status.checking_resume_data: "checking",
        }
        return states.get(state, "unknown")

    def get_torrent(self, info_hash: str) -> Optional[TorrentInfo]:
        """Get single torrent info."""
        torrents = self.list_torrents()
        for t in torrents:
            if t.info_hash == info_hash:
                return t
        return None

    def pause(self, info_hash: str) -> bool:
        """Pause a torrent."""
        handle = self.torrents.get(info_hash)
        if handle:
            handle.pause()
            return True
        return False

    def resume(self, info_hash: str) -> bool:
        """Resume a torrent."""
        handle = self.torrents.get(info_hash)
        if handle:
            handle.resume()
            return True
        return False

    def remove(self, info_hash: str, delete_files: bool = False) -> bool:
        """Remove a torrent."""
        handle = self.torrents.get(info_hash)
        if handle:
            if delete_files:
                self.session.remove_torrent(handle, lt.options_t.delete_files)
            else:
                self.session.remove_torrent(handle)
            del self.torrents[info_hash]
            self._update_numbering()
            return True
        return False

    def get_files(self, info_hash: str) -> list[dict]:
        """Get file list for a torrent."""
        handle = self.torrents.get(info_hash)
        if not handle:
            return []

        info = handle.torrent_file()
        if not info:
            return []

        files = []
        for i in range(info.num_files()):
            f = info.files().file_path(i)
            s = info.files().file_size(i)
            files.append({"path": f, "size": s})

        return files

    # ==================== SCGI Server for Flood ====================

    def _run_scgi_server(self):
        """Run SCGI server for Flood compatibility."""
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.settimeout(1.0)

        try:
            server.bind((self.scgi_host, self.scgi_port))
            server.listen(5)
            logger.info(f"SCGI server listening on {self.scgi_host}:{self.scgi_port}")

            while self._scgi_running:
                try:
                    client, addr = server.accept()
                    threading.Thread(
                        target=self._handle_scgi_client,
                        args=(client,),
                        daemon=True
                    ).start()
                except socket.timeout:
                    continue
                except Exception as e:
                    logger.error(f"SCGI accept error: {e}")
        finally:
            server.close()

    def _handle_scgi_client(self, client: socket.socket):
        """Handle an SCGI client connection."""
        try:
            # Read SCGI request
            data = b""
            while True:
                chunk = client.recv(4096)
                if not chunk:
                    break
                data += chunk
                if b"</methodCall>" in data:
                    break

            if not data:
                return

            # Parse SCGI header (netstring format: "length:headers,body")
            colon_idx = data.find(b":")
            if colon_idx > 0:
                header_len = int(data[:colon_idx])
                header_end = colon_idx + 1 + header_len + 1  # +1 for : and ,
                body = data[header_end:]
            else:
                body = data

            # Parse XML-RPC request
            try:
                # Find XML start
                xml_start = body.find(b"<?xml")
                if xml_start == -1:
                    xml_start = body.find(b"<methodCall>")
                if xml_start >= 0:
                    body = body[xml_start:]

                params, method = xmlrpc.client.loads(body.decode('utf-8'))
                result = self._handle_xmlrpc(method, params)

                # Create XML-RPC response
                response = xmlrpc.client.dumps((result,), methodresponse=True)
            except Exception as e:
                logger.error(f"XML-RPC parse error: {e}")
                response = xmlrpc.client.dumps(
                    xmlrpc.client.Fault(1, str(e)),
                    methodresponse=True
                )

            # Send response
            client.sendall(response.encode('utf-8'))
        except Exception as e:
            logger.error(f"SCGI handler error: {e}")
        finally:
            client.close()

    def _handle_xmlrpc(self, method: str, params: tuple):
        """Handle an XML-RPC method call (rtorrent-compatible)."""
        logger.debug(f"XML-RPC: {method} {params}")

        # System methods
        if method == "system.listMethods":
            return [
                "system.listMethods",
                "system.client_version",
                "d.multicall2",
                "load.raw_start",
                "d.start", "d.stop", "d.erase",
                "d.name", "d.size_bytes", "d.completed_bytes",
                "d.down.rate", "d.up.rate", "d.hash",
            ]

        elif method == "system.client_version":
            return f"libtorrent/{lt.__version__}"

        # Torrent list (Flood uses this)
        elif method == "d.multicall2":
            # params: ("", "main", "d.hash=", "d.name=", ...)
            view = params[1] if len(params) > 1 else "main"
            fields = params[2:] if len(params) > 2 else []

            result = []
            for info_hash, handle in self.torrents.items():
                try:
                    status = handle.status()
                    row = []
                    for field in fields:
                        field = field.rstrip("=")
                        value = self._get_torrent_field(handle, status, field)
                        row.append(value)
                    result.append(row)
                except Exception as e:
                    logger.error(f"Error in multicall for {info_hash}: {e}")

            return result

        # Load torrent (raw .torrent data)
        elif method == "load.raw_start":
            # params: ("", torrent_data_base64)
            if len(params) >= 2:
                torrent_data = params[1]
                if isinstance(torrent_data, xmlrpc.client.Binary):
                    torrent_data = torrent_data.data
                self.add_torrent_file(torrent_data)
            return 0

        # Single torrent operations
        elif method == "d.start":
            if params:
                self.resume(params[0])
            return 0

        elif method == "d.stop":
            if params:
                self.pause(params[0])
            return 0

        elif method == "d.erase":
            if params:
                self.remove(params[0])
            return 0

        # Torrent field getters
        elif method.startswith("d."):
            if params:
                handle = self.torrents.get(params[0])
                if handle:
                    status = handle.status()
                    return self._get_torrent_field(handle, status, method)
            return ""

        else:
            logger.warning(f"Unknown XML-RPC method: {method}")
            return ""

    def _get_torrent_field(self, handle, status, field: str):
        """Get a torrent field value (rtorrent-compatible names)."""
        field_map = {
            "d.hash": lambda: str(handle.info_hash()),
            "d.name": lambda: status.name or "",
            "d.size_bytes": lambda: status.total_wanted,
            "d.completed_bytes": lambda: status.total_wanted_done,
            "d.down.rate": lambda: status.download_rate,
            "d.up.rate": lambda: status.upload_rate,
            "d.down.total": lambda: status.total_download,
            "d.up.total": lambda: status.total_upload,
            "d.ratio": lambda: (status.total_upload / max(status.total_download, 1)) * 1000,
            "d.is_active": lambda: 1 if not status.paused else 0,
            "d.is_open": lambda: 1,
            "d.state": lambda: 1 if not status.paused else 0,
            "d.complete": lambda: 1 if status.is_finished else 0,
            "d.hashing": lambda: 0,
            "d.message": lambda: "",
            "d.priority": lambda: 2,
            "d.peers_connected": lambda: status.num_peers,
            "d.peers_complete": lambda: status.num_seeds,
            "d.directory": lambda: status.save_path,
            "d.base_path": lambda: status.save_path,
            "d.left_bytes": lambda: status.total_wanted - status.total_wanted_done,
            "d.creation_date": lambda: 0,
            "d.timestamp.started": lambda: 0,
            "d.timestamp.finished": lambda: 0,
            "d.custom1": lambda: "",
            "d.custom2": lambda: "",
            "d.custom3": lambda: "",
            "d.custom4": lambda: "",
            "d.custom5": lambda: "",
        }

        getter = field_map.get(field)
        if getter:
            return getter()

        logger.debug(f"Unknown field: {field}")
        return ""


def format_torrent_list(torrents: list[TorrentInfo]) -> str:
    """Format torrent list as markdown."""
    if not torrents:
        return "No torrents."

    lines = ["**Torrents:**\n"]
    for i, t in enumerate(torrents, 1):
        # Progress bar
        bar_len = 10
        filled = int(t.progress / 100 * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)

        # Speed
        down = f"{t.download_rate / 1024:.1f} KB/s" if t.download_rate > 0 else "-"
        up = f"{t.upload_rate / 1024:.1f} KB/s" if t.upload_rate > 0 else "-"

        # Size
        size_mb = t.size / (1024 * 1024)
        size_str = f"{size_mb:.1f} MB" if size_mb < 1024 else f"{size_mb/1024:.2f} GB"

        # State icon
        state_icon = {
            "downloading": "⬇️",
            "seeding": "⬆️",
            "finished": "✅",
            "paused": "⏸️",
            "checking": "🔍",
            "metadata": "📥",
        }.get(t.state, "❓")

        lines.append(
            f"{i}. {state_icon} **{t.name}**\n"
            f"   [{bar}] {t.progress:.1f}% | {size_str}\n"
            f"   ↓{down} ↑{up} | {t.seeders}S/{t.peers}P"
        )

    return "\n".join(lines)
