"""
Built-in Tor client service.
Uses system Tor binary with configurable exit nodes.
"""

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
    ):
        self.listen_host = listen_host
        self.socks_port = socks_port
        self.control_port = control_port
        self.dns_port = dns_port
        self.exit_nodes = exit_nodes
        self.data_dir = Path(data_dir)

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
StrictNodes 1

# Performance
CircuitBuildTimeout 30
LearnCircuitBuildTimeout 0
MaxCircuitDirtiness 600

# Logging
Log notice file {self.data_dir}/tor.log

# Disable unnecessary features
AvoidDiskWrites 1
"""
        logger.info(f"[TOR] Creating torrc: SOCKS {self.listen_host}:{self.socks_port}, DNS {self.listen_host}:{self.dns_port}, exits={self.exit_nodes}")
        torrc_path.write_text(config)
        return torrc_path

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
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
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
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
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
                    # Backoff: 5s, 10s, 20s, ... capped at 60s
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
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
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


def start_tor_service(
    listen_host: str = "127.0.0.1",
    socks_port: int = 9052,
    control_port: int = 9053,
    dns_port: int = 9055,
    exit_nodes: str = "{us}",
    data_dir: str = "/var/lib/posterchanai/tor",
) -> Optional[TorService]:
    """Start Tor service and return instance."""
    service = TorService.get_instance(
        listen_host=listen_host,
        socks_port=socks_port,
        control_port=control_port,
        dns_port=dns_port,
        exit_nodes=exit_nodes,
        data_dir=data_dir,
    )
    if service.start():
        return service
    return None


def stop_tor_service():
    """Stop Tor service if running."""
    if TorService._instance:
        TorService._instance.stop()
