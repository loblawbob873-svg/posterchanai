"""
Built-in torrent client using libtorrent with HTTP proxy support.
All traffic is routed through the configured HTTP proxy (for Tor).
"""

import sys
import threading
import socket
from pathlib import Path
from dataclasses import dataclass
from typing import Optional
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

        # Configure session settings for STRICT Tor-only operation:
        # ALL peer connections MUST go through HTTP proxy -> Tor
        # NO direct connections allowed (UDP disabled entirely)
        # IPv4 ONLY - SOCKS4 doesn't support IPv6
        settings = {
            'alert_mask': lt.alert.category_t.all_categories,
            # IPv4 only - no IPv6 (SOCKS4 doesn't support it, would leak)
            'listen_interfaces': f'0.0.0.0:{listen_port}',
            'download_rate_limit': 0,  # unlimited
            'upload_rate_limit': 0,
            # DISABLE DHT - uses UDP which CANNOT go through HTTP proxy
            'enable_dht': False,
            # DISABLE LSD - local network discovery, not useful for Tor
            'enable_lsd': False,
            # DISABLE uTP - uses UDP which CANNOT go through HTTP proxy
            'enable_outgoing_utp': False,
            'enable_incoming_utp': False,
            # TCP only - this goes through HTTP proxy
            'enable_outgoing_tcp': True,
            'enable_incoming_tcp': True,
            # Announce to all trackers (HTTP only will work through proxy)
            'announce_to_all_trackers': True,
            'announce_to_all_tiers': True,
            'connections_limit': 200,
            'active_downloads': -1,
            'active_seeds': -1,
            'active_limit': -1,
            # Disable IPv6 - SOCKS4/HTTP proxy doesn't support it
            'enable_ip_notifier': False,
        }

        # REQUIRE proxy - no torrenting without Tor
        if not proxy_host:
            raise ValueError("Proxy is REQUIRED for torrenting. Configure HTTP proxy in Admin Settings.")

        # Verify proxy is reachable before starting - retry with delays
        # This allows the proxy server to start after the torrent service
        proxy_available = False
        max_retries = 5
        retry_delay = 2  # seconds
        
        for attempt in range(max_retries):
            if self._check_proxy(proxy_host, proxy_port):
                proxy_available = True
                break
            if attempt < max_retries - 1:
                logger.warning(f"[BT] Proxy check failed (attempt {attempt + 1}/{max_retries}), retrying in {retry_delay}s...")
                import time
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
        
        if not proxy_available:
            logger.warning(f"[BT] Cannot connect to proxy at {proxy_host}:{proxy_port} after {max_retries} attempts.")
            logger.warning(f"[BT] Torrent service will start but torrenting will be disabled until proxy is available.")
            logger.warning(f"[BT] Proxy will be checked periodically and torrenting will be enabled automatically.")
            # Don't raise - allow service to start, but mark proxy as unavailable
            self._proxy_available = False
        else:
            self._proxy_available = True

        self.proxy_host = proxy_host
        self.proxy_port = proxy_port

        # Only configure proxy settings if proxy is available
        if self._proxy_available:
            settings.update({
                'proxy_type': lt.proxy_type_t.http,
                'proxy_hostname': proxy_host,
                'proxy_port': proxy_port,
                # Don't force ALL traffic - allow trackers direct access
                'force_proxy': False,
                # CRITICAL: Force peer DATA connections through Tor
                'proxy_peer_connections': True,
                # Allow direct tracker connections (UDP trackers need this)
                'proxy_tracker_connections': False,
                # Allow direct DNS for trackers
                'proxy_hostnames': False,
                # Anonymous mode - don't leak peer_id, client info
                'anonymous_mode': True,
            })
        else:
            # Proxy not available - configure but don't enable proxy settings
            # The service will start but torrenting will be disabled
            logger.warning(f"[BT] Starting without proxy - torrenting will be disabled until proxy is available")

        # Log startup configuration
        if self._proxy_available:
            logger.info(f"[BT] ========== TORRENT ENGINE STARTING (TOR DATA MODE) ==========")
            logger.info(f"[BT] HTTP Proxy: {proxy_host}:{proxy_port} -> Tor SOCKS5")
            logger.info(f"[BT] Download dir: {self.download_dir}")
            logger.info(f"[BT] Trackers: DIRECT (UDP+HTTP work) - IP visible to trackers")
            logger.info(f"[BT] Peer DATA: PROXIED through Tor - anonymous transfers")
            logger.info(f"[BT] DHT: DISABLED (peer-to-peer UDP)")
            logger.info(f"[BT] uTP: DISABLED (peer-to-peer UDP)")
            logger.info(f"[BT] Anonymous mode: ENABLED")
            logger.info(f"[BT] =============================================================")
        else:
            logger.warning(f"[BT] ========== TORRENT ENGINE STARTING (PROXY UNAVAILABLE) ==========")
            logger.warning(f"[BT] HTTP Proxy: {proxy_host}:{proxy_port} - NOT REACHABLE")
            logger.warning(f"[BT] Download dir: {self.download_dir}")
            logger.warning(f"[BT] Torrenting DISABLED - waiting for proxy to become available")
            logger.warning(f"[BT] Proxy will be checked periodically and enabled automatically")
            logger.warning(f"[BT] =============================================================")

        self.session.apply_settings(settings)

        # Track torrents by hash
        self.torrents: dict[str, lt.torrent_handle] = {}
        self._number_to_hash: dict[int, str] = {}  # For user-friendly numbering

        # Resume data directory
        self.resume_dir = self.download_dir / ".resume"
        self.resume_dir.mkdir(parents=True, exist_ok=True)

        # Persisted set of info_hashes we've already sent a "download complete"
        # Telegram alert for, so frequent restarts/rechecks (which re-emit
        # torrent_finished_alert) don't re-spam. Cleared per-torrent on remove().
        self._notified_path = self.resume_dir / ".notified.json"
        self._notified: set[str] = self._load_notified()

        # Persisted info_hash -> user_id of whoever added each torrent, so the
        # "download complete" alert goes to that user (any user can add torrents).
        # Survives restarts (resume data carries no owner). Unknown owner → admins.
        self._owners_path = self.resume_dir / ".owners.json"
        self._owners: dict[str, int] = self._load_owners()

        # The .notified / .owners JSON files are written from BOTH the alert thread
        # (torrent_finished / restore) and request handlers (add/remove), so serialize
        # every write: an interleaved/partial write corrupts the JSON and, once the
        # .notified file is unreadable, every completed torrent re-spams its "download
        # complete" alert after the next restart. This lock + atomic temp-file replace
        # in _save_notified/_save_owners prevents that.
        self._file_lock = threading.Lock()
        self._remove_lock = threading.Lock()   # guards the destructive remove() re-resolve — NOT the class
                                                # singleton lock (reusing that risks a deadlock vs get_instance)

        # In-session tombstones: info_hashes removed by remove() this uptime. A late
        # save_resume_data_alert (periodic request still in flight when the torrent was
        # removed) must NOT rewrite the .resume file — that resurrects the torrent on the
        # next restart. We key the skip on "was explicitly removed" rather than "absent from
        # self.torrents" so an ACTIVE torrent whose _stable_ih happens to differ from its dict
        # key (v1/v2 hybrid drift) is still saved. Cleared per-hash on re-add; reset on restart
        # (a removed torrent has no .resume to resurrect after a restart anyway).
        self._removed: set[str] = set()

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

        # Join the alert thread BEFORE saving so _save_resume_data is the sole consumer of
        # session.pop_alerts(). Otherwise the still-running _process_alerts thread can drain a
        # save_resume_data_alert first, leaving its hash stuck in `pending` and blocking
        # shutdown for the full 10s timeout on every restart/deploy.
        t = getattr(self, "_alert_thread", None)
        if t is not None:
            t.join(timeout=5)

        # Save resume data for all torrents
        self._save_resume_data()

        # Save session state
        self.session.pause()

        logger.info("[BT] LibtorrentService stopped")

    def _stable_ih(self, handle) -> str:
        """A STABLE info-hash string used as the dict key AND the resume filename. The deprecated
        `handle.info_hash()` (v1 accessor) returns ALL-ZEROS for a v2/hybrid torrent whose metadata
        resolved v2-first — so its resume file gets saved as 0000…0000.resume but later reloads under
        the REAL hash, and remove() (keyed by the real hash) can never delete it, so the torrent comes
        back every restart ('Orange Is the New Black' bug). Prefer a non-zero v1, then v2, so the hash
        used everywhere is consistent and never all-zeros for a real torrent."""
        try:
            ihs = handle.info_hashes()
            v1 = getattr(ihs, "v1", None)
            if v1 is not None and not v1.is_all_zeros():
                return str(v1)
            v2 = getattr(ihs, "v2", None)
            if v2 is not None and not v2.is_all_zeros():
                return str(v2)
        except Exception:
            pass
        return str(handle.info_hash())

    def _save_resume_data(self):
        """Save resume data for all torrents."""
        logger.info("[BT] Saving resume data for all torrents...")
        count = 0
        for info_hash, handle in list(self.torrents.items()):
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
                    info_hash = self._stable_ih(alert.handle)
                    # Skip late alerts for torrents removed just before shutdown (a periodic
                    # save request still in flight) — writing them here resurrects the .resume
                    # file so the removed torrent comes back on the next restart. Mirrors the
                    # same tombstone guard in _process_alerts.
                    if info_hash in self._removed:
                        continue
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
                    info_hash = self._stable_ih(alert.handle)
                    pending.discard(info_hash)
                    logger.warning(f"[BT] Resume data failed for {info_hash}: {alert.error}")
            time.sleep(0.1)

        logger.info(f"[BT] Saved resume data for {count} torrents")

    def _load_resume_data(self):
        """Load resume data and re-add torrents."""
        if not self.resume_dir.exists():
            return

        count = 0
        resumed = 0
        paused = 0
        for resume_file in self.resume_dir.glob("*.resume"):
            try:
                resume_data = resume_file.read_bytes()
                params = lt.read_resume_data(resume_data)
                params.save_path = str(self.download_dir)

                # libtorrent encodes the paused flag into the resume data, so a torrent the user
                # paused before the restart was saved paused. Honour that saved state instead of
                # blindly resuming everything (the old behaviour forced every torrent back to
                # "running" on restart, losing the user's paused state). Apply it explicitly after
                # the add so the result doesn't depend on libtorrent's default flag merging.
                was_paused = bool(params.flags & lt.torrent_flags.paused)

                handle = self.session.add_torrent(params)
                info_hash = self._stable_ih(handle)
                self.torrents[info_hash] = handle
                count += 1

                # SELF-HEAL: an older resume file may be named by a hash that no longer matches this
                # torrent's stable hash (the all-zeros / v1-vs-v2 drift that let a removed torrent come
                # back). Rename it to the stable hash so a future remove() finds and deletes it.
                if resume_file.name != f"{info_hash}.resume":
                    try:
                        resume_file.replace(self.resume_dir / f"{info_hash}.resume")
                        logger.info(f"[BT] Renamed stale resume {resume_file.name} -> {info_hash}.resume")
                    except Exception as e:
                        logger.warning(f"[BT] Could not rename resume {resume_file.name}: {e}")

                if handle.is_valid():
                    if was_paused:
                        handle.unset_flags(lt.torrent_flags.auto_managed)
                        handle.pause()
                        paused += 1
                    else:
                        handle.set_flags(lt.torrent_flags.auto_managed)
                        handle.resume()
                        resumed += 1
                logger.debug(f"[BT] Restored torrent ({'paused' if was_paused else 'running'}): {info_hash}")
            except Exception as e:
                logger.error(f"[BT] Failed to load resume data from {resume_file}: {e}")
                # Do NOT delete the resume file on a load error: a transient/version/add-torrent error would
                # then PERMANENTLY destroy the torrent. Leave it in place so a later run can retry/recover.

        if count > 0:
            self._update_numbering()
            logger.info(f"[BT] Restored {count} torrents from resume data ({resumed} running, {paused} paused)")

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
        if not self._proxy_available:
            # Check if proxy has become available
            if self._check_proxy(self.proxy_host, self.proxy_port):
                logger.info(f"[BT] Proxy is now available! Enabling torrenting...")
                self._enable_proxy()
                self._proxy_available = True
            else:
                raise ConnectionError(f"Proxy at {self.proxy_host}:{self.proxy_port} is not available. Torrenting blocked.")
        elif not self._check_proxy(self.proxy_host, self.proxy_port):
            # Proxy was available but is now down
            logger.warning(f"[BT] Proxy became unavailable! Disabling torrenting...")
            self._proxy_available = False
            raise ConnectionError(f"Proxy at {self.proxy_host}:{self.proxy_port} is not available. Torrenting blocked.")
    
    def _enable_proxy(self):
        """Enable proxy settings in libtorrent session."""
        import libtorrent as lt
        settings = {
            'proxy_type': lt.proxy_type_t.http,
            'proxy_hostname': self.proxy_host,
            'proxy_port': self.proxy_port,
            'force_proxy': False,
            'proxy_peer_connections': True,
            'proxy_tracker_connections': False,
            'proxy_hostnames': False,
            'anonymous_mode': True,
        }
        self.session.apply_settings(settings)
        logger.info(f"[BT] Proxy settings enabled: {self.proxy_host}:{self.proxy_port}")
    
    def _recheck_all_torrents(self):
        """Recheck all torrents when proxy becomes available."""
        if not self.torrents:
            return
        
        logger.info(f"[BT] Rechecking {len(self.torrents)} torrent(s) now that proxy is available...")
        rechecked = 0
        for info_hash, handle in list(self.torrents.items()):
            try:
                if handle.is_valid():
                    handle.force_recheck()
                    rechecked += 1
                    logger.debug(f"[BT] Rechecking torrent: {info_hash[:8]}...")
            except Exception as e:
                logger.error(f"[BT] Failed to recheck torrent {info_hash[:8]}...: {e}")
        
        if rechecked > 0:
            logger.info(f"[BT] Recheck initiated for {rechecked} torrent(s)")

    def _process_alerts(self):
        """Process libtorrent alerts in background with detailed logging."""
        proxy_check_counter = 0
        resume_save_counter = 0
        proxy_was_down = False

        while self._running:
            # Periodic resume-data save (~every 30s = 15 * 2s). Without this, resume data was only
            # saved on a clean shutdown — so a kill/restart (or our frequent deploys) dropped every
            # torrent added since the last save. Now a restart loses at most ~30s of progress, never
            # the torrents themselves (they re-add from .resume on next start).
            resume_save_counter += 1
            if resume_save_counter >= 15:
                resume_save_counter = 0
                for _ih, _h in list(self.torrents.items()):
                    try:
                        if _h.is_valid():
                            _h.save_resume_data(lt.torrent_handle.save_info_dict)
                    except Exception as e:
                        logger.debug(f"[BT] periodic resume request failed for {_ih}: {e}")

            # Periodic proxy check - every 60 iterations (~30 seconds)
            proxy_check_counter += 1
            if proxy_check_counter >= 60:
                proxy_check_counter = 0
                proxy_ok = self._check_proxy(self.proxy_host, self.proxy_port)
                
                # If proxy was unavailable at startup, check if it's now available
                if not self._proxy_available and proxy_ok:
                    logger.info(f"[BT] Proxy is now available! Enabling torrenting...")
                    self._enable_proxy()
                    self._proxy_available = True
                    proxy_was_down = False
                    # Recheck all torrents now that proxy is available
                    self._recheck_all_torrents()
                elif self._proxy_available and not proxy_ok and not proxy_was_down:
                    # Proxy went down - pause all active torrents for safety
                    logger.warning(f"[BT] PROXY DOWN! Pausing all torrents for anonymity protection.")
                    proxy_was_down = True
                    self._proxy_available = False
                    for info_hash, handle in list(self.torrents.items()):
                        try:
                            if not (handle.flags() & lt.torrent_flags.paused):
                                handle.unset_flags(lt.torrent_flags.auto_managed)
                                handle.pause()
                                logger.warning(f"[BT] Auto-paused: {handle.status().name}")
                        except Exception as e:
                            logger.error(f"[BT] Error pausing {info_hash}: {e}")
                elif proxy_ok and proxy_was_down:
                    logger.info(f"[BT] Proxy restored. Torrents remain paused - resume manually.")
                    proxy_was_down = False

            alerts = self.session.pop_alerts()
            for alert in alerts:
                alert_type = type(alert).__name__

                # Resume-data saves (periodic + shutdown): write the .resume file as the alert
                # arrives, so persistence happens continuously in the background — not only in the
                # shutdown-only _save_resume_data (which raced the loop and saved partial state).
                if isinstance(alert, lt.save_resume_data_alert):
                    try:
                        info_hash = self._stable_ih(alert.handle)
                        # Ignore late resume-save alerts for torrents removed between the
                        # periodic save request and this alert — otherwise the .resume file
                        # is resurrected and the torrent comes back on the next restart.
                        # Tombstone-keyed (not "in self.torrents") so an active torrent whose
                        # _stable_ih drifts from its dict key is still persisted.
                        if info_hash not in self._removed:
                            (self.resume_dir / f"{info_hash}.resume").write_bytes(
                                lt.write_resume_data_buf(alert.params))
                    except Exception as e:
                        logger.error(f"[BT] Failed to write resume data: {e}")
                    continue
                elif isinstance(alert, lt.save_resume_data_failed_alert):
                    logger.debug(f"[BT] Resume save failed: {getattr(alert, 'error', '')}")
                    continue

                # Torrent lifecycle events
                if isinstance(alert, lt.torrent_finished_alert):
                    name = alert.torrent_name
                    logger.info(f"[BT] FINISHED: {name}")
                    # Notify once per torrent that the download is done. Dedup against the
                    # persisted set so restarts/rechecks (which re-emit this alert) don't re-spam.
                    try:
                        ih = self._stable_ih(alert.handle)
                        if ih in self.torrents and ih not in self._notified:
                            self._notified.add(ih)
                            self._save_notified()
                            self._notify_finished(ih, name)
                    except Exception as e:
                        logger.error(f"[BT] finish-notify error: {e}")
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

            # 2s sleep to avoid burning a full CPU core when many torrents/alerts
            time.sleep(2.0)

    def add_magnet(self, magnet: str, user_id: Optional[int] = None) -> str:
        """Add a magnet link. Returns info_hash. Requires proxy."""
        # Verify proxy is still available before adding
        self._verify_proxy_or_fail()

        params = lt.parse_magnet_uri(magnet)
        params.save_path = str(self.download_dir)

        handle = self.session.add_torrent(params)
        info_hash = self._stable_ih(handle)

        self.torrents[info_hash] = handle
        self._removed.discard(info_hash)  # re-add clears any prior in-session tombstone
        self._update_numbering()
        self._set_owner(info_hash, user_id)

        logger.info(f"Added magnet: {info_hash}")
        return info_hash

    def add_torrent_file(self, torrent_data: bytes, user_id: Optional[int] = None) -> str:
        """Add a .torrent file. Returns info_hash. Requires proxy."""
        # Verify proxy is still available before adding
        self._verify_proxy_or_fail()

        info = lt.torrent_info(lt.bdecode(torrent_data))

        params = lt.add_torrent_params()
        params.ti = info
        params.save_path = str(self.download_dir)

        handle = self.session.add_torrent(params)
        info_hash = self._stable_ih(handle)

        self.torrents[info_hash] = handle
        self._removed.discard(info_hash)  # re-add clears any prior in-session tombstone
        self._update_numbering()
        self._set_owner(info_hash, user_id)

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

        # Log session status for debugging
        try:
            session_status = self.session.status()
            logger.info(f"[BT] Session: DHT={session_status.dht_nodes} nodes, "
                       f"DL={session_status.download_rate/1024:.1f}KB/s, "
                       f"UL={session_status.upload_rate/1024:.1f}KB/s, "
                       f"torrents={len(self.torrents)}")
        except Exception as e:
            logger.error(f"[BT] Failed to get session status: {e}")

        for info_hash, handle in list(self.torrents.items()):
            try:
                status = handle.status()
                info = handle.torrent_file()

                # Debug log each torrent state (debug level to avoid spam)
                logger.debug(f"[BT] {status.name[:30]}: state={self._state_str(status.state)}, "
                            f"peers={status.num_peers}, seeds={status.num_seeds}, "
                            f"progress={status.progress*100:.1f}%")

                # Calculate ETA
                eta = -1
                if status.download_rate > 0:
                    remaining = status.total_wanted - status.total_wanted_done
                    eta = int(remaining / status.download_rate)

                # Check paused state - multiple methods for reliability
                flags = handle.flags()
                is_paused_flag = bool(flags & lt.torrent_flags.paused)
                is_auto_managed = bool(flags & lt.torrent_flags.auto_managed)

                # Torrent is paused if: paused flag is set, or not auto-managed with 0 rates
                # Also check status.paused in older libtorrent versions
                is_paused = is_paused_flag or (hasattr(status, 'paused') and status.paused)

                logger.debug(f"[BT] {status.name[:20]}: paused_flag={is_paused_flag}, auto_managed={is_auto_managed}, is_paused={is_paused}")

                # Use "paused" state if paused, otherwise normal state
                state = "paused" if is_paused else self._state_str(status.state)

                # Force 0 rates for paused torrents (status might show stale values)
                dl_rate = 0 if is_paused else status.download_rate
                ul_rate = 0 if is_paused else status.upload_rate

                result.append(TorrentInfo(
                    info_hash=info_hash,
                    name=status.name or "Unknown",
                    size=status.total_wanted,
                    downloaded=status.total_wanted_done,
                    uploaded=status.total_upload,
                    progress=status.progress * 100,
                    download_rate=dl_rate,
                    upload_rate=ul_rate,
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
            # Disable auto-manage so session doesn't resume it
            handle.unset_flags(lt.torrent_flags.auto_managed)
            handle.pause()
            # Persist the paused state now so a restart within the ~30s periodic-save window still
            # brings it back paused (the alert handler writes the .resume file when this completes).
            try:
                handle.save_resume_data(lt.torrent_handle.save_info_dict)
            except Exception as e:
                logger.debug(f"[BT] resume-data request after pause failed: {e}")
            logger.info(f"[BT] Paused torrent: {handle.status().name}")
            return True
        return False

    def resume(self, info_hash: str) -> bool:
        """Resume a torrent. Requires proxy to be available."""
        # Verify proxy is available before resuming - don't allow downloads without Tor
        self._verify_proxy_or_fail()

        handle = self.torrents.get(info_hash)
        if handle:
            handle.resume()
            # Re-enable auto-manage
            handle.set_flags(lt.torrent_flags.auto_managed)
            # Persist the running state now so a restart soon after still brings it back running.
            try:
                handle.save_resume_data(lt.torrent_handle.save_info_dict)
            except Exception as e:
                logger.debug(f"[BT] resume-data request after resume failed: {e}")
            logger.info(f"[BT] Resumed torrent: {handle.status().name}")
            return True
        return False

    def _load_notified(self) -> set:
        """Load the persisted set of info_hashes already alerted as 'download complete'."""
        try:
            if self._notified_path.exists():
                import json
                return set(json.loads(self._notified_path.read_text()))
        except Exception as e:
            logger.debug(f"[BT] could not load notified set: {e}")
        return set()

    def _save_notified(self) -> None:
        # Atomic + serialized: write a temp file then os.replace() it into place under the
        # shared file lock, so concurrent writers (alert thread + request handlers) can never
        # leave a half-written .notified.json that would re-spam completion alerts on restart.
        try:
            import os, json
            with self._file_lock:
                tmp = self._notified_path.with_name(self._notified_path.name + ".tmp")
                tmp.write_text(json.dumps(sorted(self._notified)))
                os.replace(tmp, self._notified_path)
        except Exception as e:
            logger.error(f"[BT] could not save notified set: {e}")

    def _load_owners(self) -> dict:
        """Load the persisted info_hash -> user_id ownership map."""
        try:
            if self._owners_path.exists():
                import json
                return {str(k): int(v) for k, v in json.loads(self._owners_path.read_text()).items()}
        except Exception as e:
            logger.debug(f"[BT] could not load owners map: {e}")
        return {}

    def _save_owners(self) -> None:
        # Same atomic + serialized write as _save_notified (written from _set_owner on add and
        # from remove(), both racing the alert thread) to avoid a corrupt .owners.json.
        try:
            import os, json
            with self._file_lock:
                tmp = self._owners_path.with_name(self._owners_path.name + ".tmp")
                tmp.write_text(json.dumps(self._owners))
                os.replace(tmp, self._owners_path)
        except Exception as e:
            logger.error(f"[BT] could not save owners map: {e}")

    def _set_owner(self, info_hash: str, user_id: Optional[int]) -> None:
        """Record who added a torrent so its completion alert reaches that user."""
        if user_id is None:
            return
        self._owners[info_hash] = int(user_id)
        self._save_owners()

    def _notify_finished(self, info_hash: str, name: str) -> None:
        """A torrent finished downloading → file a 'complete' reminder for the user who added
        it (any user can add torrents); fall back to admins if the owner is unknown. The reminder
        scheduler then delivers it to BOTH the web UI (⏰ Reminders conversation + live websocket)
        AND Telegram (~30s), reusing the existing notification path."""
        try:
            from datetime import datetime
            from app.database import SessionLocal
            from app.models import User
            from app.services.reminder_service import create_reminder
            db = SessionLocal()
            try:
                owner_id = self._owners.get(info_hash)
                recipients = []
                if owner_id is not None:
                    u = db.query(User).filter(User.id == owner_id).first()
                    if u:
                        recipients = [u]
                if not recipients:
                    # Unknown owner (e.g. added before this feature) → notify admins.
                    recipients = db.query(User).filter(User.is_admin == True).all()  # noqa: E712
                if not recipients:
                    return
                text = f"🎉 Torrent finished downloading: {name}"
                for u in recipients:
                    create_reminder(db, u, text, datetime.utcnow())
                logger.info(f"[BT] filed torrent-complete reminder for {len(recipients)} user(s): {name}")
            finally:
                db.close()
        except Exception as e:
            logger.error(f"[BT] failed to file torrent-complete reminder: {e}")

    def remove(self, info_hash: str, delete_files: bool = False) -> bool:
        """Remove a torrent and its resume data.

        DESTRUCTIVE. Callers reach this from a user-facing positional number (`rm N` / `rmf N`)
        that `_update_numbering` reindexes on every add/remove, so a stale number can name the
        WRONG torrent — and `delete_files=True` is irreversible. We therefore act ONLY on the
        stable `info_hash` (never a position) and re-resolve it to a live handle under the lock
        immediately before removal, so a concurrent add/remove can't retarget the delete. The
        volatile number→hash resolution itself lives in the callers; keep it out of this method.
        """
        with self._remove_lock:   # dedicated lock — NOT the class singleton-construction lock (self._lock)
            return self._remove_locked(info_hash, delete_files)

    @staticmethod
    def _removed_forms(handle, info_hash: str) -> set[str]:
        """Every hash string this torrent can be known by — the dict key plus its v1 and v2 hashes."""
        forms = {info_hash}
        try:
            ihs = handle.info_hashes()
            for _h in (getattr(ihs, "v1", None), getattr(ihs, "v2", None)):
                if _h is not None and not _h.is_all_zeros():
                    forms.add(str(_h))
        except Exception:
            pass
        return forms

    def _purge_resume_files(self, forms: set[str]):
        """Delete every .resume file belonging to a torrent, whatever it happens to be named.

        Matched by NAME first, then by CONTENT — an all-zeros (or otherwise drifted) filename says
        nothing about which torrent is inside, and that file is the one that resurrects a removal.
        Reading it is cheap: there is at most one per torrent, and this runs once per removal.
        """
        for f in self.resume_dir.glob("*.resume"):
            try:
                mine = f.stem in forms
                if not mine:
                    try:
                        p = lt.read_resume_data(f.read_bytes())
                        ihs = p.info_hashes
                        for _h in (getattr(ihs, "v1", None), getattr(ihs, "v2", None)):
                            if _h is not None and not _h.is_all_zeros() and str(_h) in forms:
                                mine = True
                                break
                    except Exception:
                        # Unreadable: leave it. A file we cannot identify may belong to a torrent the
                        # user still has, and deleting THAT is the worse mistake of the two.
                        continue
                if mine:
                    f.unlink()
                    logger.info(f"[BT] Deleted resume file: {f.name}")
            except Exception as e:
                logger.error(f"[BT] Failed to delete resume file {f.name}: {e}")

    def _remove_locked(self, info_hash: str, delete_files: bool) -> bool:
        # Re-fetch under the lock (see remove() docstring): the mapping is validated here, at the
        # last instant before the irreversible session.remove_torrent call, not at resolve time.
        handle = self.torrents.get(info_hash)
        if handle:
            # Tombstone every hash form BEFORE removal so a late resume-save alert (whichever
            # hash _stable_ih resolves it to) can't rewrite the .resume file and resurrect it.
            forms = self._removed_forms(handle, info_hash)
            self._removed |= forms
            if delete_files:
                self.session.remove_torrent(handle, lt.options_t.delete_files)
            else:
                self.session.remove_torrent(handle)
            del self.torrents[info_hash]
            self._update_numbering()

            # Delete EVERY resume file this torrent owns, not just the one named by its dict key.
            # A file written by an older build can sit under a different name (see _stable_ih:
            # 0000…0000.resume for a v2/hybrid), and unlinking one name leaves the other on disk —
            # where the next start globs it, re-adds the torrent, and only THEN renames it to the
            # stable hash. The tombstone above is in-memory, so it does not survive that restart.
            # Measured in production: REMOVED at 20:30, service restarted at 22:02, "Renamed stale
            # resume 0000…0000.resume -> 6543d5d0….resume" + "Restored 1 torrents" — and the torrent
            # was back, having been removed twice.
            self._purge_resume_files(forms)

            # Forget the completion alert so re-adding the same torrent notifies again.
            if info_hash in self._notified:
                self._notified.discard(info_hash)
                self._save_notified()
            if info_hash in self._owners:
                del self._owners[info_hash]
                self._save_owners()

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

        # Clear status with icon AND text
        if t.is_paused or t.state == "paused":
            status = "⏸️ **PAUSED**"
        elif t.state == "downloading":
            status = "⬇️ **DOWNLOADING**"
        elif t.state == "seeding":
            status = "⬆️ **SEEDING**"
        elif t.state == "finished":
            status = "✅ **FINISHED**"
        elif t.state == "checking":
            status = "🔍 **CHECKING**"
        elif t.state == "metadata":
            status = "📥 **FETCHING METADATA**"
        else:
            status = f"❓ **{t.state.upper()}**"

        # Action buttons - clear labels
        if t.is_paused or t.state == "paused":
            toggle_btn = f"[▶ Resume](cmd:torrents resume {i})"
        else:
            toggle_btn = f"[⏸ Pause](cmd:torrents pause {i})"
        delete_btn = f"[🗑 Remove](cmd:torrents rm {i})"

        lines.append(
            f"**{i}. {t.name}**\n"
            f"   Status: {status}\n"
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

        state = t.get("state", "unknown")
        is_paused = t.get("is_paused", False)

        # Clear status with icon AND text
        if is_paused or state == "paused":
            status = "⏸️ **PAUSED**"
        elif state == "downloading":
            status = "⬇️ **DOWNLOADING**"
        elif state == "seeding":
            status = "⬆️ **SEEDING**"
        elif state == "finished":
            status = "✅ **FINISHED**"
        elif state == "checking":
            status = "🔍 **CHECKING**"
        elif state == "metadata":
            status = "📥 **FETCHING METADATA**"
        else:
            status = f"❓ **{state.upper()}**"

        name = t.get("name", "Unknown")
        seeders = t.get("seeders", 0)
        peers = t.get("peers", 0)

        # Action buttons - clear labels
        if is_paused or state == "paused":
            toggle_btn = f"[▶ Resume](cmd:torrents resume {i})"
        else:
            toggle_btn = f"[⏸ Pause](cmd:torrents pause {i})"
        delete_btn = f"[🗑 Remove](cmd:torrents rm {i})"

        lines.append(
            f"**{i}. {name}**\n"
            f"   Status: {status}\n"
            f"   [{bar}] {progress:.1f}% | {size_str}\n"
            f"   ↓{down} ↑{up} | {seeders}S/{peers}P\n"
            f"   {toggle_btn} | {delete_btn}"
        )

    return "\n".join(lines)
