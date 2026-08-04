"""
Built-in Tor client service.
Uses system Tor binary with configurable exit nodes.
"""

import ipaddress
import os
import socket
import subprocess
import threading
import logging
import time
import shutil
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class TorService:
    """
    Manages a Tor SOCKS5 proxy using the system Tor binary.
    Supports exit node country selection and control port.
    """

    _instance: Optional['TorService'] = None
    _lock = threading.Lock()

    def __init__(
        self,
        listen_host: str = "127.0.0.1",
        socks_port: int = 9052,
        control_port: int = 9053,
        dns_port: int = 9055,
        exit_nodes: str = "{us}",
        data_dir: str = "/var/lib/posterchanai/tor",
        onion_enabled: bool = False,
        onion_target: str = "",
        onion_relay_port: int = 0,
    ):
        self.listen_host = listen_host
        self.socks_port = socks_port
        self.control_port = control_port
        self.dns_port = dns_port
        self.exit_nodes = exit_nodes
        self.data_dir = Path(data_dir)
        # Onion (v3 hidden service): exposes the app over Tor at a persistent .onion address. The keys
        # live in <data_dir>/onion_service (persist across restarts → same address). onion_target is
        # the local "host:port" to forward to (the app's port).
        self.onion_enabled = onion_enabled
        self.onion_target = onion_target
        # The Nostr relay is a SEPARATE server on its own port — in production nginx routes /relay to it,
        # but a hidden service forwards TCP, not paths, so port 80 alone leaves the onion with no relay
        # and the whole client dead. Map the relay's port through the same onion too, so the client can
        # reach ws://<onion>:<relay_port>/relay. 0 = don't publish it.
        self.onion_relay_port = int(onion_relay_port or 0)
        self.onion_dir = self.data_dir / "onion_service"

        self._process: Optional[subprocess.Popen] = None
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._restart_failures = 0

    @classmethod
    def get_instance(
        cls,
        listen_host: str = "127.0.0.1",
        socks_port: int = 9052,
        control_port: int = 9053,
        dns_port: int = 9055,
        exit_nodes: str = "{us}",
        data_dir: str = "/var/lib/posterchanai/tor",
    ) -> 'TorService':
        """Get or create singleton instance."""
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(
                    listen_host=listen_host,
                    socks_port=socks_port,
                    control_port=control_port,
                    dns_port=dns_port,
                    exit_nodes=exit_nodes,
                    data_dir=data_dir,
                )
            return cls._instance

    def _find_tor_binary(self) -> Optional[str]:
        """Find the Tor binary."""
        paths = [
            "/usr/bin/tor",
            "/usr/local/bin/tor",
            "/opt/homebrew/bin/tor",
            shutil.which("tor"),
        ]
        for path in paths:
            if path and os.path.isfile(path) and os.access(path, os.X_OK):
                return path
        return None

    def _create_torrc(self) -> Path:
        """Create torrc configuration file."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        torrc_path = self.data_dir / "torrc"

        config = f"""# Posterchanai Tor instance
SocksPort {self.listen_host}:{self.socks_port}
ControlPort {self.listen_host}:{self.control_port}
DNSPort {self.listen_host}:{self.dns_port}
DataDirectory {self.data_dir}

# Control port authentication (for nyx monitoring)
CookieAuthentication 1

# Exit node restrictions
ExitNodes {self.exit_nodes}

# Performance
CircuitBuildTimeout 30
LearnCircuitBuildTimeout 0
MaxCircuitDirtiness 600
SocksTimeout 30

# Logging -> STDOUT, so it lands in the journal under this unit and journald owns rotation and
# rate-limiting. The old `Log notice file .../tor.log` had NEITHER: tor does not rotate its own log,
# so a single torrenting session wrote a 166 MB file of one repeated line —
#   "All routers are down or won't exit -- choosing a doomed exit at random"
# — 417k times, because torrent traffic to port 6881 through a country-restricted exit set finds
# almost no exit willing to carry it and rebuilds circuits forever. Unbounded, and it survives every
# restart because nothing ever truncates it.
#
# Level stays `notice`: it is where tor reports bootstrap progress, and "why won't tor start" is much
# harder to answer without it. journald rate-limits a repeated line instead of writing it 417k times,
# which is the actual problem — not the level.
Log notice stdout

# Disable unnecessary features
AvoidDiskWrites 1
"""
        if self.onion_enabled and self.onion_target:
            # v3 hidden service exposing the app over Tor. Keys persist in onion_dir → stable .onion.
            config += f"""
# Hidden service (.onion) for this deployment
HiddenServiceDir {self.onion_dir}
HiddenServiceVersion 3
HiddenServicePort 80 {self.onion_target}
"""
            if self.onion_relay_port:
                config += f"HiddenServicePort {self.onion_relay_port} 127.0.0.1:{self.onion_relay_port}\n"
        logger.info(f"[TOR] Creating torrc: SOCKS {self.listen_host}:{self.socks_port}, DNS {self.listen_host}:{self.dns_port}, exits={self.exit_nodes}, onion={'on' if self.onion_enabled else 'off'}")
        torrc_path.write_text(config)
        return torrc_path

    def get_onion_address(self):
        """The .onion hostname Tor generated for the hidden service, or None if not up yet."""
        try:
            hn = (self.onion_dir / "hostname").read_text().strip()
            return hn or None
        except Exception:
            return None

    def reload_onion(self, enabled: bool, target: str = "", relay_port: int = 0) -> bool:
        """Turn the hidden service on/off LIVE: rewrite the torrc and SIGHUP Tor — it reloads its
        config and creates (or drops) the .onion without a full process restart. Keys in onion_dir
        persist, so re-enabling yields the SAME address. Returns True if the reload signal was sent."""
        self.onion_enabled = enabled
        if target:
            self.onion_target = target
        if relay_port:
            self.onion_relay_port = int(relay_port)
        try:
            self._create_torrc()
            if self._process and self._process.poll() is None:
                import signal
                os.kill(self._process.pid, signal.SIGHUP)
                logger.info(f"[TOR] reloaded torrc via SIGHUP (onion={'on' if enabled else 'off'})")
                return True
        except Exception as e:
            logger.error(f"[TOR] reload_onion failed: {e}")
        return False

    def start(self) -> bool:
        """Start the Tor process. Returns immediately; bootstrap completes in background."""
        if self._running:
            logger.warning("[TOR] Already running")
            return True

        tor_binary = self._find_tor_binary()
        if not tor_binary:
            logger.error("[TOR] Tor binary not found. Install: apt install tor")
            return False

        try:
            torrc_path = self._create_torrc()

            self._process = subprocess.Popen(
                [tor_binary, "-f", str(torrc_path)],
                # INHERIT the parent's stdout/stderr — do NOT pipe and do NOT discard.
                #   DEVNULL threw tor's own log away entirely, which is what `Log notice stdout` in
                #   the torrc feeds. The bootstrap lines you still see are OUR python logger, not
                #   tor's, so a tor that fails to bootstrap would say nothing anywhere.
                #   subprocess.PIPE would be worse: nothing reads it, so the 64 KB pipe buffer fills
                #   and TOR BLOCKS — a hang, not a lost log.
                # Inheriting sends it to the journal under this unit (systemd) or to `docker logs`
                # (container), both of which rotate and rate-limit. That is the whole point of moving
                # off the unrotated 526 MB file.
                stdout=None,
                stderr=None,
                start_new_session=True,
            )

            self._running = True
            logger.info(f"[TOR] Process started (PID {self._process.pid}) - SOCKS5 will be on {self.listen_host}:{self.socks_port}")

            # Bootstrap check and process monitoring both run in the background
            self._monitor_thread = threading.Thread(target=self._monitor_and_bootstrap, daemon=True)
            self._monitor_thread.start()

            return True

        except Exception as e:
            logger.error(f"[TOR] Failed to start: {e}")
            return False

    def _monitor_and_bootstrap(self):
        """Background thread: wait for bootstrap then monitor for crashes."""
        if self._wait_for_bootstrap():
            logger.info(f"[TOR] Bootstrapped - SOCKS5 ready on {self.listen_host}:{self.socks_port}")
            self._restart_failures = 0
        else:
            logger.warning("[TOR] Bootstrap timed out — Tor may still connect later")

        self._monitor()

    def _read_cookie(self) -> Optional[bytes]:
        """Read the control auth cookie file."""
        cookie_path = self.data_dir / "control_auth_cookie"
        try:
            if cookie_path.exists():
                return cookie_path.read_bytes()
        except Exception as e:
            logger.debug(f"[TOR] Could not read cookie: {e}")
        return None

    def _authenticate(self, sock) -> bool:
        """Authenticate to control port using cookie or empty auth."""
        cookie = self._read_cookie()
        if cookie:
            auth_cmd = b'AUTHENTICATE ' + cookie.hex().encode() + b'\r\n'
        else:
            auth_cmd = b'AUTHENTICATE ""\r\n'

        sock.send(auth_cmd)
        response = sock.recv(1024)
        return b'250' in response

    def _wait_for_bootstrap(self, timeout: int = 120) -> bool:
        """Wait for Tor to bootstrap via control port."""
        start_time = time.time()
        last_log = 0
        while time.time() - start_time < timeout:
            if not self._running or not self._process:
                return False

            try:
                family = socket.AF_INET6 if isinstance(ipaddress.ip_address(self.listen_host), ipaddress.IPv6Address) else socket.AF_INET
                sock = socket.socket(family, socket.SOCK_STREAM)
                sock.settimeout(5)
                try:
                    sock.connect((self.listen_host, self.control_port))
                    if self._authenticate(sock):
                        sock.send(b'GETINFO status/bootstrap-phase\r\n')
                        response = sock.recv(1024)
                        if b'Bootstrapped 100%' in response or b'TAG=done' in response:
                            logger.info("[TOR] Bootstrap complete")
                            return True
                        # Log progress every 15s instead of spamming every 2s
                        elapsed = time.time() - start_time
                        if elapsed - last_log >= 15:
                            logger.info(f"[TOR] Still bootstrapping... ({int(elapsed)}s elapsed)")
                            last_log = elapsed
                finally:
                    sock.close()
            except Exception:
                pass

            time.sleep(5)

        logger.warning(f"[TOR] Bootstrap did not complete in {timeout}s — process will keep running")
        return False

    def _monitor(self):
        """Monitor Tor process and restart if needed."""
        while self._running and self._process:
            ret = self._process.poll()
            if ret is not None:
                logger.warning(f"[TOR] Process exited with code: {ret}")
                if self._running:
                    self._restart_failures += 1
                    # Backoff: 5s, 10s, 15s, ... capped at 60s
                    delay = min(5 * self._restart_failures, 60)
                    logger.info(f"[TOR] Restarting in {delay}s (attempt {self._restart_failures})...")
                    time.sleep(delay)
                    self._running = False
                    self._process = None
                    self.start()
                break
            time.sleep(5)

    def stop(self):
        """Stop the Tor process."""
        self._running = False

        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
            except Exception as e:
                logger.error(f"[TOR] Stop error: {e}")
            finally:
                self._process = None

        logger.info("[TOR] Stopped")

    def get_new_identity(self) -> bool:
        """Request a new Tor circuit."""
        try:
            family = socket.AF_INET6 if isinstance(ipaddress.ip_address(self.listen_host), ipaddress.IPv6Address) else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(10)
            try:
                sock.connect((self.listen_host, self.control_port))
                if self._authenticate(sock):
                    sock.send(b'SIGNAL NEWNYM\r\n')
                    response = sock.recv(1024)
                    if b'250' in response:
                        logger.info("[TOR] New identity")
                        return True
            finally:
                sock.close()
        except Exception as e:
            logger.error(f"[TOR] New identity failed: {e}")
        return False

    def is_running(self) -> bool:
        """Check if Tor is running."""
        return self._running and self._process is not None and self._process.poll() is None

    def get_status(self) -> dict:
        """Get Tor status."""
        return {
            "running": self.is_running(),
            "socks_port": self.socks_port,
            "control_port": self.control_port,
            "dns_port": self.dns_port,
            "exit_nodes": self.exit_nodes,
            "listen_host": self.listen_host,
        }


# All started Tor instances (1 normally, 2 when the second daemon is on for SOCKS load-balancing).
# NOT a singleton — each instance needs its OWN ports + data dir, so we track them in a list.
_services: list = []


def start_tor_service(
    listen_host: str = "127.0.0.1",
    socks_port: int = 9052,
    control_port: int = 9053,
    dns_port: int = 9055,
    exit_nodes: str = "{us}",
    data_dir: str = "/var/lib/posterchanai/tor",
    onion_enabled: bool = False,
    onion_target: str = "",
    onion_relay_port: int = 0,
) -> Optional[TorService]:
    """Start ONE Tor instance and return it. Call once per daemon — the second daemon uses its own
    ports + data dir + exit region so the HTTP proxy can load-balance across two independent circuits.
    onion_* (primary daemon only) exposes the app at a persistent .onion address."""
    service = TorService(
        listen_host=listen_host,
        socks_port=socks_port,
        control_port=control_port,
        dns_port=dns_port,
        exit_nodes=exit_nodes,
        data_dir=data_dir,
        onion_enabled=onion_enabled,
        onion_target=onion_target,
        onion_relay_port=onion_relay_port,
    )
    if service.start():
        _services.append(service)
        if TorService._instance is None:
            TorService._instance = service   # first instance = the default (status/monitoring compat)
        return service
    return None


def stop_tor_service():
    """Stop ALL started Tor instances."""
    for svc in _services:
        try:
            svc.stop()
        except Exception:
            pass
    _services.clear()
    TorService._instance = None


def primary_service():
    """The first/primary Tor instance — it hosts the deployment's .onion hidden service."""
    return _services[0] if _services else None


def set_onion(enabled: bool, target: str = "", relay_port: int = 0):
    """Enable/disable the deployment's .onion on the primary daemon (live SIGHUP reload). Returns the
    .onion address (may be None on the very first enable — Tor needs a moment; poll get_onion_address).

    When tor runs in its OWN unit this process holds no daemon handle, so primary_service() is None
    and the live reload silently did nothing: the admin toggle looked like it worked and the .onion
    never changed. Restart the owning unit instead — the setting is already persisted, so the fresh
    daemon comes up with the new torrc."""
    from app.role import owns as _owns, restart_owner_by_cmdline
    if not _owns("tor"):
        restart_owner_by_cmdline("--role tor")
        return get_onion_address_global()

    svc = primary_service()
    if not svc:
        return None
    svc.reload_onion(enabled, target, relay_port)
    return svc.get_onion_address()


def get_onion_address():
    """The deployment's current .onion address, or None.

    Falls back to READING THE HOSTNAME FILE when this process holds no daemon handle, which on a
    role-split deployment is every process except the tor one. Without that fallback the admin page
    asked the APP process — where `_services` is empty, so this returned None — and rendered
    "starting… Tor is generating the address (a few seconds)" **forever**, on a deployment whose
    .onion had existed for six weeks. `set_onion()` already knew about this case and used the global
    read; the READ path did not, which is the whole bug: the address was never missing, only
    unreachable from the process that was asked for it.

    The file is the truth in either case — tor writes it, and it persists across restarts (that is
    what makes the address stable), so preferring the handle buys nothing.
    """
    svc = primary_service()
    return (svc.get_onion_address() if svc else None) or get_onion_address_global()


def request_onion_host(request) -> str:
    """The onion hostname THIS request came in on, or "" if it didn't.

    Callers use it to hand an onion visitor onion-flavoured URLs (relay, Blossom, media) instead of
    the admin-configured clearnet ones — otherwise the .onion site is a facade that immediately
    pushes every socket, every blob and every upload back out an exit node, which is both broken
    (an onion-only client can't reach them at all) and a deanonymisation hazard.

    Host is client-controlled, so it must MATCH the address Tor actually generated for us — an
    arbitrary `Host: evil.onion` must never become a URL we hand out.
    """
    try:
        host = (request.headers.get("host") or request.url.netloc or "").strip().lower()
    except Exception:
        return ""
    host = host.split(":")[0]
    if not host.endswith(".onion"):
        return ""
    ours = (get_onion_address() or "").strip().lower()
    return host if ours and host == ours else ""


def start_from_settings() -> bool:
    """Start the Tor daemon(s) this node is configured for. Returns True if the primary came up.

    EXTRACTED from app/main.py's startup so the `tor` role process can start exactly the same thing
    the app used to. Deliberately one implementation: a copy in the role runner would drift from the
    app's the first time either changed — the failure mode the duplicated own-media-hosts list had.
    """
    import os as _os
    from app.services import settings_store as _ss
    if not _ss.get_bool("tor_enabled"):
        return False
    listen_host = _ss.get("tor_listen_host", "127.0.0.1")
    socks_port = int(_ss.get("tor_socks_port", "9052"))
    control_port = _ss.get_int("tor_control_port", 9053)
    _app_port = _os.getenv("POSTERCHANAI_PORT", "3051")   # the .onion forwards Tor -> the app
    primary = start_tor_service(
        listen_host=listen_host,
        socks_port=socks_port,
        control_port=control_port,
        dns_port=_ss.get_int("tor_dns_port", control_port + 2),
        exit_nodes=_ss.get("tor_exit_nodes", "{us}"),
        data_dir=_ss.get("tor_data_dir", "/var/lib/posterchanai/tor"),
        onion_enabled=_ss.get_bool("onion_enabled"),
        onion_target=f"127.0.0.1:{_app_port}",
        onion_relay_port=_ss.get_int("nostr_relay_port", 3052),
    )
    logger.info("[TOR] built-in Tor %s (SOCKS5 on %s:%s)",
                "started" if primary else "FAILED to start", listen_host, socks_port)
    # Second daemon (different exit region) so the HTTP proxy can load-balance across two independent
    # circuits / exit IPs — dodges per-IP rate limits + geo-blocks. Own ports + data dir; DNS derives
    # from its control port (+2), like #1.
    if _ss.get_bool("tor2_enabled"):
        t2_control = _ss.get_int("tor2_control_port", 9063)
        t2_socks = _ss.get_int("tor2_socks_port", 9062)
        t2_exits = _ss.get("tor2_exit_nodes", "{ca}")
        ok2 = start_tor_service(
            listen_host=listen_host, socks_port=t2_socks, control_port=t2_control,
            dns_port=t2_control + 2, exit_nodes=t2_exits,
            data_dir=_ss.get("tor2_data_dir", "/var/lib/posterchanai/tor2"))
        logger.info("[TOR] built-in Tor #2 %s (SOCKS5 on %s:%s, exits=%s)",
                    "started" if ok2 else "FAILED to start", listen_host, t2_socks, t2_exits)
    return bool(primary)


def get_onion_address_global():
    """The .onion hostname from the primary daemon's hostname FILE — readable from any process,
    unlike the in-memory service handle."""
    from app.services import settings_store as _ss
    from pathlib import Path
    try:
        d = Path(_ss.get("tor_data_dir", "/var/lib/posterchanai/tor")) / "onion_service"
        hn = (d / "hostname").read_text().strip()
        return hn or None
    except Exception:
        return None
