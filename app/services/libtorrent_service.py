"""
Built-in torrent client using libtorrent with HTTP proxy support.
All traffic is routed through the configured HTTP proxy (for Tor).
"""

import sys
import threading
import socket
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Callable
import logging
import time

logger = logging.getLogger(__name__)

# Try to import libtorrent, falling back to system site-packages if needed
try:
    import libtorrent as lt
except ImportError:
    # Try system site-packages (for venv without --system-site-packages)
    import glob
    system_paths = glob.glob("/usr/lib/python3*/site-packages")
    for sp in system_paths:
        if sp not in sys.path:
            sys.path.insert(0, sp)
    try:
        import libtorrent as lt
        logger.info(f"[BT] Loaded libtorrent from system site-packages")
    except ImportError:
        raise ImportError(
            "libtorrent not found. Install system package:\n"
            "  Gentoo: emerge net-libs/libtorrent-rasterbar\n"
            "  Debian: apt install python3-libtorrent"
        )


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
    - HTTP proxy REQUIRED for Tor (force_proxy=True)
    - All traffic routed through proxy - no direct connections
    """

    _instance: Optional['LibtorrentService'] = None
    _lock = threading.Lock()

    def __init__(
        self,
        download_dir: str = "/tmp/torrents",
        proxy_host: str = "",
        proxy_port: int = 8118,
        listen_port: int = 6881,
    ):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)

        # Create libtorrent session
        self.session = lt.session()

        # Configure session settings - mirror rtorrent config:
        # system.daemon.set = true (we run as part of the app)
        # trackers.use_udp.set = yes (enable, proxy will block/fallback)
        # dht.mode.set = auto (enable, proxy will block/fallback)
        settings = {
            'alert_mask': lt.alert.category_t.all_categories,
            'listen_interfaces': f'0.0.0.0:{listen_port}',
            'download_rate_limit': 0,  # unlimited
            'upload_rate_limit': 0,
            # dht.mode.set = auto - enable DHT, will fallback if proxy blocks UDP
            'enable_dht': True,
            'dht_bootstrap_nodes': 'router.bittorrent.com:6881,router.utorrent.com:6881,dht.transmissionbt.com:6881',
            # Enable LSD (Local Service Discovery)
            'enable_lsd': True,
            # PEX (Peer Exchange) is enabled by default in libtorrent 2.x
            # Enable uTP - proxy will force TCP fallback
            'enable_outgoing_utp': True,
            'enable_incoming_utp': True,
            'enable_outgoing_tcp': True,
            'enable_incoming_tcp': True,
            # trackers.use_udp.set = yes - enable UDP trackers, HTTP proxy forces HTTP fallback
            'announce_to_all_trackers': True,
            'announce_to_all_tiers': True,
            # Performance settings
            'connections_limit': 200,
            'active_downloads': 8,
            'active_seeds': 5,
            'active_limit': 15,
        }

        # REQUIRE proxy - no direct connections allowed
        if not proxy_host:
            raise ValueError("Proxy is REQUIRED for torrenting. Configure HTTP proxy in Admin Settings.")

        # Verify proxy is reachable before starting
        if not self._check_proxy(proxy_host, proxy_port):
            raise ConnectionError(f"Cannot connect to proxy at {proxy_host}:{proxy_port}. Torrenting disabled.")

        self.proxy_host = proxy_host
        self.proxy_port = proxy_port

        settings.update({
            'proxy_type': lt.proxy_type_t.http,
            'proxy_hostname': proxy_host,
            'proxy_port': proxy_port,
            'proxy_peer_connections': True,
            'proxy_tracker_connections': True,
            'proxy_hostnames': True,
            'force_proxy': True,  # Force ALL traffic through proxy - NO DIRECT CONNECTIONS
            # Additional safety: disable features that might leak
            'anonymous_mode': True,  # Hide client identity
        })

        # Log startup configuration
        logger.info(f"[BT] ========== TORRENT ENGINE STARTING ==========")
        logger.info(f"[BT] Proxy: {proxy_host}:{proxy_port} (REQUIRED - ALL TRAFFIC)")
        logger.info(f"[BT] force_proxy: True (no direct connections)")
        logger.info(f"[BT] anonymous_mode: True (identity hidden)")
        logger.info(f"[BT] proxy_peer_connections: True")
        logger.info(f"[BT] proxy_tracker_connections: True")
        logger.info(f"[BT] Download dir: {self.download_dir}")
        logger.info(f"[BT] Listen port: {listen_port}")
        logger.info(f"[BT] DHT: enabled (will use proxy)")
        logger.info(f"[BT] UDP trackers: enabled (will fallback to HTTP via proxy)")
        logger.info(f"[BT] ==============================================")

        self.session.apply_settings(settings)

        # Track torrents by hash
        self.torrents: dict[str, lt.torrent_handle] = {}
        self._number_to_hash: dict[int, str] = {}  # For user-friendly numbering

        # Resume data directory
        self.resume_dir = self.download_dir / ".resume"
        self.resume_dir.mkdir(parents=True, exist_ok=True)

        # Alert processing
        self._alert_thread: Optional[threading.Thread] = None
        self._running = False

    @classmethod
    def get_instance(
        cls,
        download_dir: str = "/tmp/torrents",
        proxy_host: str = "",
        proxy_port: int = 8118,
        listen_port: int = 6881,
    ) -> 'LibtorrentService':
        """Get or create singleton instance."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(
                    download_dir=download_dir,
                    proxy_host=proxy_host,
                    proxy_port=proxy_port,
                    listen_port=listen_port,
                )
                cls._instance.start()
            return cls._instance

    def start(self):
        """Start background threads."""
        if self._running:
            return

        self._running = True

        # Load saved torrents from resume data
        self._load_resume_data()

        # Start alert processing thread
        self._alert_thread = threading.Thread(target=self._process_alerts, daemon=True)
        self._alert_thread.start()

        logger.info(f"[BT] LibtorrentService started")

    def stop(self):
        """Stop background threads and save resume data."""
        self._running = False

        # Save resume data for all torrents
        self._save_resume_data()

        # Save session state
        self.session.pause()

        logger.info("[BT] LibtorrentService stopped")

    def _save_resume_data(self):
        """Save resume data for all torrents."""
        logger.info("[BT] Saving resume data for all torrents...")
        count = 0
        for info_hash, handle in self.torrents.items():
            try:
                if not handle.is_valid():
                    continue
                # Request save resume data
                handle.save_resume_data(lt.torrent_handle.save_info_dict)
            except Exception as e:
                logger.error(f"[BT] Failed to request resume data for {info_hash}: {e}")

        # Process save_resume_data_alert
        timeout = time.time() + 10  # 10 second timeout
        pending = set(self.torrents.keys())
        while pending and time.time() < timeout:
            alerts = self.session.pop_alerts()
            for alert in alerts:
                if isinstance(alert, lt.save_resume_data_alert):
                    info_hash = str(alert.handle.info_hash())
                    try:
                        resume_file = self.resume_dir / f"{info_hash}.resume"
                        resume_data = lt.write_resume_data_buf(alert.params)
                        resume_file.write_bytes(resume_data)
                        count += 1
                        pending.discard(info_hash)
                        logger.debug(f"[BT] Saved resume data: {info_hash}")
                    except Exception as e:
                        logger.error(f"[BT] Failed to write resume data for {info_hash}: {e}")
                elif isinstance(alert, lt.save_resume_data_failed_alert):
                    info_hash = str(alert.handle.info_hash())
                    pending.discard(info_hash)
                    logger.warning(f"[BT] Resume data failed for {info_hash}: {alert.error}")
            time.sleep(0.1)

        logger.info(f"[BT] Saved resume data for {count} torrents")

    def _load_resume_data(self):
        """Load resume data and re-add torrents."""
        if not self.resume_dir.exists():
            return

        count = 0
        for resume_file in self.resume_dir.glob("*.resume"):
            try:
                resume_data = resume_file.read_bytes()
                params = lt.read_resume_data(resume_data)
                params.save_path = str(self.download_dir)

                handle = self.session.add_torrent(params)
                info_hash = str(handle.info_hash())
                self.torrents[info_hash] = handle
                count += 1
                logger.debug(f"[BT] Restored torrent: {info_hash}")
            except Exception as e:
                logger.error(f"[BT] Failed to load resume data from {resume_file}: {e}")
                # Remove corrupted resume file
                try:
                    resume_file.unlink()
                except:
                    pass

        if count > 0:
            logger.info(f"[BT] Restored {count} torrents from resume data")
            self._update_numbering()
            # Auto-resume all torrents on startup
            resumed = 0
            for info_hash, handle in self.torrents.items():
                try:
                    if handle.is_valid():
                        handle.resume()
                        resumed += 1
                except Exception as e:
                    logger.error(f"[BT] Failed to resume {info_hash}: {e}")
            if resumed > 0:
                logger.info(f"[BT] Auto-resumed {resumed} torrents")

    def _check_proxy(self, host: str, port: int, timeout: int = 5) -> bool:
        """Verify proxy is reachable and responding."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((host, port))
            sock.close()
            return result == 0
        except Exception as e:
            logger.error(f"Proxy check failed: {e}")
            return False

    def _verify_proxy_or_fail(self):
        """Verify proxy is still available, raise if not."""
        if not self._check_proxy(self.proxy_host, self.proxy_port):
            raise ConnectionError(f"Proxy at {self.proxy_host}:{self.proxy_port} is not available. Torrenting blocked.")

    def _process_alerts(self):
        """Process libtorrent alerts in background with detailed logging."""
        while self._running:
            alerts = self.session.pop_alerts()
            for alert in alerts:
                alert_type = type(alert).__name__

                # Torrent lifecycle events
                if isinstance(alert, lt.torrent_finished_alert):
                    logger.info(f"[BT] FINISHED: {alert.torrent_name}")
                elif isinstance(alert, lt.torrent_error_alert):
                    logger.error(f"[BT] ERROR: {alert.torrent_name} - {alert.error}")
                elif isinstance(alert, lt.torrent_added_alert):
                    logger.info(f"[BT] ADDED: {alert.torrent_name}")
                elif isinstance(alert, lt.torrent_removed_alert):
                    logger.info(f"[BT] REMOVED: {alert.info_hash}")
                elif isinstance(alert, lt.torrent_paused_alert):
                    logger.info(f"[BT] PAUSED: {alert.torrent_name}")
                elif isinstance(alert, lt.torrent_resumed_alert):
                    logger.info(f"[BT] RESUMED: {alert.torrent_name}")

                # Metadata and state
                elif isinstance(alert, lt.metadata_received_alert):
                    logger.info(f"[BT] METADATA: {alert.torrent_name}")
                elif isinstance(alert, lt.state_changed_alert):
                    logger.debug(f"[BT] STATE: {alert.torrent_name} -> {alert.state}")

                # Tracker events (important for debugging)
                elif isinstance(alert, lt.tracker_reply_alert):
                    logger.info(f"[BT] TRACKER OK: {alert.torrent_name} - {alert.url} ({alert.num_peers} peers)")
                elif isinstance(alert, lt.tracker_error_alert):
                    # error_message is a method in libtorrent v2
                    err_msg = alert.error_message() if callable(alert.error_message) else str(alert.error_message)
                    # UDP tracker failures are expected with HTTP proxy - log as debug
                    if 'udp://' in alert.url:
                        logger.debug(f"[BT] TRACKER UDP FAIL (expected with HTTP proxy): {alert.torrent_name} - {alert.url}")
                    else:
                        logger.warning(f"[BT] TRACKER FAIL: {alert.torrent_name} - {alert.url} - {err_msg}")
                elif isinstance(alert, lt.tracker_warning_alert):
                    warn_msg = alert.warning_message() if callable(alert.warning_message) else str(alert.warning_message)
                    logger.warning(f"[BT] TRACKER WARN: {alert.torrent_name} - {warn_msg}")

                # DHT events
                elif isinstance(alert, lt.dht_bootstrap_alert):
                    logger.info(f"[BT] DHT: Bootstrap complete")
                elif hasattr(lt, 'dht_error_alert') and isinstance(alert, lt.dht_error_alert):
                    logger.warning(f"[BT] DHT ERROR: {alert.error}")

                # Peer events (debug level - verbose)
                elif isinstance(alert, lt.peer_connect_alert):
                    logger.debug(f"[BT] PEER CONNECT: {alert.torrent_name} - {alert.endpoint}")
                elif isinstance(alert, lt.peer_disconnected_alert):
                    logger.debug(f"[BT] PEER DISCONNECT: {alert.torrent_name} - {alert.endpoint}")
                elif isinstance(alert, lt.peer_error_alert):
                    logger.debug(f"[BT] PEER ERROR: {alert.torrent_name} - {alert.error}")

                # Connection/proxy issues (important!)
                elif isinstance(alert, lt.listen_failed_alert):
                    logger.error(f"[BT] LISTEN FAILED: {alert.error}")
                elif isinstance(alert, lt.portmap_error_alert):
                    logger.warning(f"[BT] PORTMAP ERROR: {alert.error}")
                elif isinstance(alert, lt.udp_error_alert):
                    logger.debug(f"[BT] UDP ERROR (expected with proxy): {alert.error}")

                # File events
                elif isinstance(alert, lt.file_completed_alert):
                    logger.info(f"[BT] FILE DONE: {alert.torrent_name} - file {alert.index}")
                elif isinstance(alert, lt.storage_moved_alert):
                    logger.info(f"[BT] MOVED: {alert.torrent_name} -> {alert.storage_path}")

                # Log unknown important alerts at debug level
                elif 'error' in alert_type.lower() or 'fail' in alert_type.lower():
                    logger.warning(f"[BT] {alert_type}: {alert.message()}")

            time.sleep(0.5)

    def add_magnet(self, magnet: str) -> str:
        """Add a magnet link. Returns info_hash. Requires proxy."""
        # Verify proxy is still available before adding
        self._verify_proxy_or_fail()

        params = lt.parse_magnet_uri(magnet)
        params.save_path = str(self.download_dir)

        handle = self.session.add_torrent(params)
        info_hash = str(handle.info_hash())

        self.torrents[info_hash] = handle
        self._update_numbering()

        logger.info(f"Added magnet: {info_hash}")
        return info_hash

    def add_torrent_file(self, torrent_data: bytes) -> str:
        """Add a .torrent file. Returns info_hash. Requires proxy."""
        # Verify proxy is still available before adding
        self._verify_proxy_or_fail()

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

                # Check paused state using handle.flags() (reliable in libtorrent v2)
                is_paused = bool(handle.flags() & lt.torrent_flags.paused)

                # Use "paused" state if paused, otherwise normal state
                state = "paused" if is_paused else self._state_str(status.state)

                result.append(TorrentInfo(
                    info_hash=info_hash,
                    name=status.name or "Unknown",
                    size=status.total_wanted,
                    downloaded=status.total_wanted_done,
                    uploaded=status.total_upload,
                    progress=status.progress * 100,
                    download_rate=status.download_rate,
                    upload_rate=status.upload_rate,
                    state=state,
                    seeders=status.num_seeds,
                    peers=status.num_peers,
                    eta=eta,
                    save_path=status.save_path,
                    is_finished=status.is_finished,
                    is_paused=is_paused,
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

        # Action buttons
        if t.is_paused or t.state == "paused":
            toggle_btn = f"[▶ Start](cmd:bt start {i})"
        else:
            toggle_btn = f"[⏸ Pause](cmd:bt pause {i})"
        delete_btn = f"[🗑 Delete](cmd:bt rm {i})"

        lines.append(
            f"{i}. {state_icon} **{t.name}**\n"
            f"   [{bar}] {t.progress:.1f}% | {size_str}\n"
            f"   ↓{down} ↑{up} | {t.seeders}S/{t.peers}P\n"
            f"   {toggle_btn} | {delete_btn}"
        )

    return "\n".join(lines)


def format_torrent_list_from_dicts(torrents: list[dict]) -> str:
    """Format torrent list from API response dicts as markdown."""
    if not torrents:
        return "No torrents."

    lines = ["**Torrents:**\n"]
    for i, t in enumerate(torrents, 1):
        # Progress bar
        bar_len = 10
        progress = t.get("progress", 0)
        filled = int(progress / 100 * bar_len)
        bar = "█" * filled + "░" * (bar_len - filled)

        # Speed
        download_rate = t.get("download_rate", 0)
        upload_rate = t.get("upload_rate", 0)
        down = f"{download_rate / 1024:.1f} KB/s" if download_rate > 0 else "-"
        up = f"{upload_rate / 1024:.1f} KB/s" if upload_rate > 0 else "-"

        # Size
        size = t.get("size", 0)
        size_mb = size / (1024 * 1024)
        size_str = f"{size_mb:.1f} MB" if size_mb < 1024 else f"{size_mb/1024:.2f} GB"

        # State icon
        state = t.get("state", "unknown")
        state_icon = {
            "downloading": "⬇️",
            "seeding": "⬆️",
            "finished": "✅",
            "paused": "⏸️",
            "checking": "🔍",
            "metadata": "📥",
        }.get(state, "❓")

        name = t.get("name", "Unknown")
        seeders = t.get("seeders", 0)
        peers = t.get("peers", 0)
        is_paused = t.get("is_paused", False)

        # Action buttons
        if is_paused or state == "paused":
            toggle_btn = f"[▶ Start](cmd:bt start {i})"
        else:
            toggle_btn = f"[⏸ Pause](cmd:bt pause {i})"
        delete_btn = f"[🗑 Delete](cmd:bt rm {i})"

        lines.append(
            f"{i}. {state_icon} **{name}**\n"
            f"   [{bar}] {progress:.1f}% | {size_str}\n"
            f"   ↓{down} ↑{up} | {seeders}S/{peers}P\n"
            f"   {toggle_btn} | {delete_btn}"
        )

    return "\n".join(lines)
